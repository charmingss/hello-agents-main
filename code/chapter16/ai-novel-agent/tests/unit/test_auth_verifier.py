from __future__ import annotations

import asyncio
import base64
import json
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any
from uuid import UUID, uuid4

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from pydantic import ValidationError

from novel_agent.api.dependencies import get_principal
from novel_agent.api.errors import InvalidTokenError, InvalidTokenReason
from novel_agent.auth.contracts import PrincipalVerifier
from novel_agent.auth.models import OidcClaims, Principal
from novel_agent.auth.verifier import OidcTokenVerifier
from novel_agent.config import Settings
from novel_agent.main import create_app

ISSUER = "https://identity.example.test/realms/novel"
AUDIENCE = "novel-api"
KID = "test-key"


def _key_material() -> tuple[rsa.RSAPrivateKey, dict[str, Any]]:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    public_key = private_key.public_key()
    jwk = json.loads(jwt.algorithms.RSAAlgorithm.to_jwk(public_key))
    jwk.update({"kid": KID, "alg": "RS256", "use": "sig"})
    return private_key, jwk


def _token(
    private_key: rsa.RSAPrivateKey,
    tenant_id: UUID,
    *,
    issuer: str = ISSUER,
    audience: str | list[str] = AUDIENCE,
    expires_at: int | None = None,
    subject: str | None = "user-123",
    include_tenant: bool = True,
    algorithm: str = "RS256",
) -> str:
    payload: dict[str, Any] = {"iss": issuer, "aud": audience}
    if expires_at is not None:
        payload["exp"] = expires_at
    if subject is not None:
        payload["sub"] = subject
    if include_tenant:
        payload["tenant_id"] = str(tenant_id)
    headers = {"kid": KID} if algorithm != "none" else None
    return jwt.encode(
        payload, private_key if algorithm != "none" else "", algorithm=algorithm, headers=headers
    )


def _token_with_raw_header(algorithm: Any) -> str:
    def segment(value: Any) -> str:
        encoded = base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=")
        return encoded.decode()

    return f"{segment({'alg': algorithm, 'kid': KID})}.{segment({})}."


class Clock:
    def __init__(self) -> None:
        self.now = 10.0

    def __call__(self) -> float:
        return self.now


def _transport(jwk: dict[str, Any], calls: list[str]) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={"issuer": ISSUER, "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs"},
            )
        if request.url.path.endswith("/protocol/openid-connect/certs"):
            return httpx.Response(200, json={"keys": [jwk]})
        return httpx.Response(404)

    return httpx.MockTransport(handler)


def test_oidc_claims_accept_aliases_and_are_frozen() -> None:
    tenant_id = uuid4()

    claims = OidcClaims.model_validate(
        {
            "sub": "user-123",
            "tenant_id": str(tenant_id),
            "aud": [AUDIENCE, "account"],
            "iss": ISSUER,
            "exp": 2_000_000_000,
            "provider_specific": "ignored",
        }
    )

    assert claims.subject == "user-123"
    assert claims.tenant_id == tenant_id
    assert claims.audience == [AUDIENCE, "account"]
    assert claims.issuer == ISSUER
    with pytest.raises(ValidationError):
        claims.subject = "other"  # type: ignore[misc]


def test_oidc_claims_require_tenant() -> None:
    with pytest.raises(ValidationError):
        OidcClaims.model_validate(
            {"sub": "user-123", "aud": AUDIENCE, "iss": ISSUER, "exp": 2_000_000_000}
        )


@pytest.mark.asyncio
async def test_valid_rs256_token_returns_principal() -> None:
    private_key, jwk = _key_material()
    tenant_id = uuid4()
    calls: list[str] = []
    async with httpx.AsyncClient(transport=_transport(jwk, calls)) as client:
        verifier = OidcTokenVerifier(ISSUER, AUDIENCE, client=client)

        principal = await verifier.verify(
            _token(private_key, tenant_id, expires_at=int(time.time()) + 300)
        )

    assert principal == Principal(subject="user-123", tenant_id=tenant_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("token_factory", "expected_reason"),
    [
        (
            lambda key, tenant: _token(
                key, tenant, audience="wrong-api", expires_at=int(time.time()) + 300
            ),
            InvalidTokenReason.AUDIENCE,
        ),
        (
            lambda key, tenant: _token(
                key,
                tenant,
                issuer="https://wrong.example.test",
                expires_at=int(time.time()) + 300,
            ),
            InvalidTokenReason.ISSUER,
        ),
        (
            lambda key, tenant: _token(key, tenant, expires_at=int(time.time()) - 300),
            InvalidTokenReason.EXPIRED,
        ),
        (
            lambda key, tenant: _token(key, tenant),
            InvalidTokenReason.CLAIMS,
        ),
        (
            lambda key, tenant: _token(
                key, tenant, expires_at=int(time.time()) + 300, include_tenant=False
            ),
            InvalidTokenReason.CLAIMS,
        ),
        (
            lambda key, tenant: _token(
                key, tenant, expires_at=int(time.time()) + 300, algorithm="none"
            ),
            InvalidTokenReason.ALGORITHM,
        ),
    ],
)
async def test_all_invalid_tokens_have_one_public_code_and_safe_diagnostics(
    token_factory: Callable[[rsa.RSAPrivateKey, UUID], str],
    expected_reason: InvalidTokenReason,
) -> None:
    private_key, jwk = _key_material()
    tenant_id = uuid4()
    token = token_factory(private_key, tenant_id)
    async with httpx.AsyncClient(transport=_transport(jwk, [])) as client:
        verifier = OidcTokenVerifier(ISSUER, AUDIENCE, client=client)

        with pytest.raises(InvalidTokenError) as caught:
            await verifier.verify(token)

    assert caught.value.reason is expected_reason
    assert caught.value.public_code == "invalid_token"
    assert token not in str(caught.value)
    assert token not in repr(caught.value)


@pytest.mark.asyncio
async def test_discovery_and_jwks_are_cached_until_ttl_expires() -> None:
    private_key, jwk = _key_material()
    tenant_id = uuid4()
    calls: list[str] = []
    clock = Clock()
    async with httpx.AsyncClient(transport=_transport(jwk, calls)) as client:
        verifier = OidcTokenVerifier(
            ISSUER, AUDIENCE, client=client, jwks_ttl_seconds=30, monotonic=clock
        )
        token = _token(private_key, tenant_id, expires_at=int(time.time()) + 300)

        await verifier.verify(token)
        await verifier.verify(token)
        assert len(calls) == 2

        clock.now += 31
        await verifier.verify(token)

    assert len(calls) == 4


@pytest.mark.asyncio
async def test_unknown_kid_forces_one_refresh_before_rejection() -> None:
    private_key, jwk = _key_material()
    other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(
        {
            "sub": "user-123",
            "tenant_id": str(uuid4()),
            "aud": AUDIENCE,
            "iss": ISSUER,
            "exp": int(time.time()) + 300,
        },
        other_key,
        algorithm="RS256",
        headers={"kid": "rotated-key"},
    )
    calls: list[str] = []
    async with httpx.AsyncClient(transport=_transport(jwk, calls)) as client:
        verifier = OidcTokenVerifier(ISSUER, AUDIENCE, client=client)
        with pytest.raises(InvalidTokenError) as caught:
            await verifier.verify(token)

    assert caught.value.reason is InvalidTokenReason.KEY
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_concurrent_unknown_kid_requests_share_one_forced_refresh() -> None:
    _, jwk = _key_material()
    unknown_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = _token(unknown_key, uuid4(), expires_at=int(time.time()) + 300)
    token = jwt.encode(
        jwt.decode(token, options={"verify_signature": False}),
        unknown_key,
        algorithm="RS256",
        headers={"kid": "unknown-key"},
    )
    calls: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        await asyncio.sleep(0.01)
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={"issuer": ISSUER, "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs"},
            )
        return httpx.Response(200, json={"keys": [jwk]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        verifier = OidcTokenVerifier(ISSUER, AUDIENCE, client=client)
        results = await asyncio.gather(
            *(verifier.verify(token) for _ in range(8)), return_exceptions=True
        )

    assert all(
        isinstance(result, InvalidTokenError) and result.reason is InvalidTokenReason.KEY
        for result in results
    )
    assert len(calls) == 4


@pytest.mark.asyncio
async def test_unknown_kid_refresh_accepts_rotated_key() -> None:
    _, old_jwk = _key_material()
    rotated_private, rotated_jwk = _key_material()
    rotated_jwk["kid"] = "rotated-key"
    token = jwt.encode(
        {
            "sub": "rotated-user",
            "tenant_id": str(uuid4()),
            "aud": AUDIENCE,
            "iss": ISSUER,
            "exp": int(time.time()) + 300,
        },
        rotated_private,
        algorithm="RS256",
        headers={"kid": "rotated-key"},
    )
    jwks_requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal jwks_requests
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={"issuer": ISSUER, "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs"},
            )
        jwks_requests += 1
        return httpx.Response(200, json={"keys": [old_jwk if jwks_requests == 1 else rotated_jwk]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        principal = await OidcTokenVerifier(ISSUER, AUDIENCE, client=client).verify(token)

    assert principal.subject == "rotated-user"
    assert jwks_requests == 2


@pytest.mark.asyncio
async def test_malformed_jwk_record_is_skipped_when_a_valid_key_follows() -> None:
    private_key, jwk = _key_material()
    malformed_jwk = {"kid": "malformed", "alg": [], "kty": []}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/.well-known/openid-configuration"):
            return httpx.Response(
                200,
                json={"issuer": ISSUER, "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs"},
            )
        return httpx.Response(200, json={"keys": [malformed_jwk, jwk]})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        principal = await OidcTokenVerifier(ISSUER, AUDIENCE, client=client).verify(
            _token(private_key, uuid4(), expires_at=int(time.time()) + 300)
        )

    assert principal.subject == "user-123"


@pytest.mark.parametrize(
    "issuer",
    ["http://identity.example.test", "https://user:secret@identity.example.test"],
)
def test_verifier_rejects_insecure_or_credentialed_issuer(issuer: str) -> None:
    with pytest.raises(ValueError):
        OidcTokenVerifier(issuer, AUDIENCE)


@pytest.mark.asyncio
async def test_verifier_rejects_off_origin_jwks_without_requesting_it() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(
            200,
            json={"issuer": ISSUER, "jwks_uri": "https://attacker.example.test/keys"},
        )

    private_key, _ = _key_material()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        verifier = OidcTokenVerifier(ISSUER, AUDIENCE, client=client)
        with pytest.raises(InvalidTokenError) as caught:
            await verifier.verify(_token(private_key, uuid4(), expires_at=int(time.time()) + 300))

    assert caught.value.reason is InvalidTokenReason.METADATA
    assert requested == [f"{ISSUER}/.well-known/openid-configuration"]


@pytest.mark.asyncio
@pytest.mark.parametrize("algorithm", [[], {}, None])
async def test_malformed_header_algorithm_has_uniform_http_401(algorithm: Any) -> None:
    def no_request(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected OIDC request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(no_request)) as oidc_client:
        verifier = OidcTokenVerifier(ISSUER, AUDIENCE, client=oidc_client)
        app = _auth_app(verifier)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as api_client:
            response = await api_client.get(
                "/me", headers={"Authorization": f"Bearer {_token_with_raw_header(algorithm)}"}
            )

    assert response.status_code == 401
    assert response.json() == {"detail": {"code": "invalid_token"}}
    assert response.headers["www-authenticate"] == "Bearer"


@pytest.mark.asyncio
@pytest.mark.parametrize("discovery_issuer", [None, [], {}])
async def test_malformed_discovery_issuer_is_metadata_error_and_uniform_401(
    discovery_issuer: Any,
) -> None:
    private_key, _ = _key_material()
    token = _token(private_key, uuid4(), expires_at=int(time.time()) + 300)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "issuer": discovery_issuer,
                "jwks_uri": f"{ISSUER}/protocol/openid-connect/certs",
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as oidc_client:
        verifier = OidcTokenVerifier(ISSUER, AUDIENCE, client=oidc_client)
        with pytest.raises(InvalidTokenError) as caught:
            await verifier.verify(token)
        assert caught.value.reason is InvalidTokenReason.METADATA

        app = _auth_app(verifier)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as api_client:
            response = await api_client.get("/me", headers={"Authorization": f"Bearer {token}"})

    assert response.status_code == 401
    assert response.json() == {"detail": {"code": "invalid_token"}}


class RecordingVerifier:
    def __init__(self, principal: Principal | None = None) -> None:
        self.tokens: list[str] = []
        self.principal = principal

    async def verify(self, token: str) -> Principal:
        self.tokens.append(token)
        if self.principal is None:
            raise InvalidTokenError(InvalidTokenReason.CLAIMS)
        return self.principal


class LifecycleVerifier(RecordingVerifier):
    def __init__(self) -> None:
        super().__init__(Principal(subject="lifecycle-user", tenant_id=uuid4()))
        self.close_calls = 0

    async def aclose(self) -> None:
        self.close_calls += 1


def _auth_app(verifier: PrincipalVerifier) -> FastAPI:
    app = create_app(oidc_verifier=verifier)

    @app.get("/me")
    async def me(
        principal: Principal = Depends(get_principal),  # noqa: B008
    ) -> dict[str, str]:
        return {"subject": principal.subject, "tenant_id": str(principal.tenant_id)}

    app.state.oidc_verifier = verifier
    return app


@pytest.mark.parametrize(
    "authorization",
    [None, "Basic abc", "Bearer", "Bearer ", "Bearer one two"],
)
def test_get_principal_rejects_missing_or_malformed_authorization(
    authorization: str | None,
) -> None:
    app = _auth_app(RecordingVerifier())
    headers = {} if authorization is None else {"Authorization": authorization}

    with TestClient(app) as client:
        response = client.get("/me", headers=headers)

    assert response.status_code == 401
    assert response.json() == {"detail": {"code": "invalid_token"}}
    assert response.headers["www-authenticate"] == "Bearer"


def test_get_principal_uses_only_bearer_token_and_not_request_tenant() -> None:
    tenant_id = uuid4()
    verifier = RecordingVerifier(Principal(subject="user-123", tenant_id=tenant_id))
    app = _auth_app(verifier)

    with TestClient(app) as client:
        response = client.get(
            "/me?tenant_id=00000000-0000-0000-0000-000000000000",
            headers={"Authorization": "bEaReR signed.jwt.value"},
        )

    assert response.status_code == 200
    assert response.json() == {"subject": "user-123", "tenant_id": str(tenant_id)}
    assert verifier.tokens == ["signed.jwt.value"]


def test_app_creates_one_verifier_before_requests_and_closes_it_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    verifier = LifecycleVerifier()
    factory_calls: list[tuple[str, str]] = []

    def factory(issuer: str, audience: str) -> LifecycleVerifier:
        factory_calls.append((issuer, audience))
        return verifier

    monkeypatch.setattr("novel_agent.main.OidcTokenVerifier", factory)
    settings = Settings(oidc_issuer=ISSUER, oidc_audience=AUDIENCE)
    app = create_app(settings)

    @app.get("/verifier-id")
    async def verifier_id(request: Request) -> dict[str, int]:
        return {"id": id(request.app.state.oidc_verifier)}

    with pytest.raises(RuntimeError, match="application failure"):
        with TestClient(app) as client:
            assert app.state.oidc_verifier is verifier
            with ThreadPoolExecutor(max_workers=4) as executor:
                responses = list(executor.map(lambda _: client.get("/verifier-id"), range(8)))
            assert {response.json()["id"] for response in responses} == {id(verifier)}
            assert factory_calls == [(ISSUER, AUDIENCE)]
            assert verifier.close_calls == 0
            raise RuntimeError("application failure")

    assert verifier.close_calls == 1


def test_app_does_not_close_injected_verifier() -> None:
    verifier = LifecycleVerifier()

    with TestClient(create_app(oidc_verifier=verifier)):
        pass

    assert verifier.close_calls == 0


def test_missing_lifespan_verifier_is_a_configuration_error() -> None:
    app = FastAPI()

    @app.get("/me")
    async def me(
        principal: Principal = Depends(get_principal),  # noqa: B008
    ) -> dict[str, str]:
        return {"subject": principal.subject}

    with TestClient(app) as client, pytest.raises(RuntimeError, match="OIDC verifier"):
        client.get("/me", headers={"Authorization": "Bearer token"})
