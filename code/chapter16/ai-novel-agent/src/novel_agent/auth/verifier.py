from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

import httpx
import jwt
from jwt import PyJWK
from pydantic import ValidationError

from novel_agent.api.errors import InvalidTokenError, InvalidTokenReason
from novel_agent.auth.models import OidcClaims, Principal

_ALLOWED_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"})


class OidcTokenVerifier:
    def __init__(
        self,
        issuer: str,
        audience: str,
        *,
        client: httpx.AsyncClient | None = None,
        jwks_ttl_seconds: float = 300,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.issuer = issuer.rstrip("/")
        self.audience = audience
        self._validate_secure_url(self.issuer)
        self._client = client or httpx.AsyncClient(timeout=5.0)
        self._owns_client = client is None
        self._ttl = min(max(float(jwks_ttl_seconds), 1.0), 86_400.0)
        self._monotonic = monotonic
        self._keys: dict[str, PyJWK] = {}
        self._expires_at = 0.0
        self._generation = 0
        self._refresh_lock = asyncio.Lock()

    async def __aenter__(self) -> OidcTokenVerifier:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def verify(self, token: str) -> Principal:
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise InvalidTokenError(InvalidTokenReason.CLAIMS, exc) from None

        algorithm = header.get("alg")
        kid = header.get("kid")
        if not isinstance(algorithm, str) or algorithm not in _ALLOWED_ALGORITHMS:
            raise InvalidTokenError(InvalidTokenReason.ALGORITHM)
        if not isinstance(kid, str) or not kid:
            raise InvalidTokenError(InvalidTokenReason.KEY)

        key = await self._find_key(kid, algorithm)
        try:
            raw_claims = jwt.decode(
                token,
                key.key,
                algorithms=[algorithm],
                audience=self.audience,
                issuer=self.issuer,
                options={"require": ["exp", "iss", "aud", "sub"]},
            )
            claims = OidcClaims.model_validate(raw_claims)
        except jwt.ExpiredSignatureError as exc:
            raise InvalidTokenError(InvalidTokenReason.EXPIRED, exc) from None
        except jwt.InvalidAudienceError as exc:
            raise InvalidTokenError(InvalidTokenReason.AUDIENCE, exc) from None
        except jwt.InvalidIssuerError as exc:
            raise InvalidTokenError(InvalidTokenReason.ISSUER, exc) from None
        except jwt.InvalidSignatureError as exc:
            raise InvalidTokenError(InvalidTokenReason.SIGNATURE, exc) from None
        except (jwt.PyJWTError, ValidationError, TypeError, ValueError) as exc:
            raise InvalidTokenError(InvalidTokenReason.CLAIMS, exc) from None

        return Principal(subject=claims.subject, tenant_id=claims.tenant_id, roles=claims.roles)

    async def _find_key(self, kid: str, algorithm: str) -> PyJWK:
        await self._refresh_if_stale()
        key = self._compatible_key(kid, algorithm)
        if key is None:
            generation = self._generation
            await self._refresh(expected_generation=generation)
            key = self._compatible_key(kid, algorithm)
        if key is None:
            raise InvalidTokenError(InvalidTokenReason.KEY)
        return key

    def _compatible_key(self, kid: str, algorithm: str) -> PyJWK | None:
        key = self._keys.get(kid)
        if key is None or key.algorithm_name != algorithm or key.key_type not in {"RSA", "EC"}:
            return None
        return key

    async def _refresh_if_stale(self) -> None:
        if self._monotonic() < self._expires_at and self._keys:
            return
        await self._refresh()

    async def _refresh(self, *, expected_generation: int | None = None) -> None:
        async with self._refresh_lock:
            if expected_generation is None:
                if self._monotonic() < self._expires_at and self._keys:
                    return
            elif self._generation != expected_generation:
                return
            try:
                discovery = await self._get_json(f"{self.issuer}/.well-known/openid-configuration")
                discovery_issuer = discovery.get("issuer")
                if not isinstance(discovery_issuer, str):
                    raise ValueError("OIDC discovery issuer must be a string")
                if discovery_issuer.rstrip("/") != self.issuer:
                    raise ValueError("OIDC discovery issuer mismatch")
                jwks_uri = discovery["jwks_uri"]
                if not isinstance(jwks_uri, str):
                    raise ValueError("OIDC jwks_uri must be a URL")
                self._validate_jwks_url(jwks_uri)
                jwks = await self._get_json(jwks_uri)
                keys = self._parse_keys(jwks["keys"])
                if not keys:
                    raise ValueError("OIDC JWKS has no supported keys")
            except httpx.HTTPError as exc:
                raise InvalidTokenError(InvalidTokenReason.NETWORK, exc) from None
            except InvalidTokenError:
                raise
            except (KeyError, TypeError, ValueError) as exc:
                raise InvalidTokenError(InvalidTokenReason.METADATA, exc) from None
            self._keys = keys
            self._expires_at = self._monotonic() + self._ttl
            self._generation += 1

    async def _get_json(self, url: str) -> dict[str, Any]:
        response = await self._client.get(url)
        response.raise_for_status()
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError("OIDC response must be an object")
        return value

    @staticmethod
    def _parse_keys(items: Any) -> dict[str, PyJWK]:
        if not isinstance(items, list):
            raise ValueError("OIDC keys must be a list")
        parsed: dict[str, PyJWK] = {}
        for item in items:
            if not isinstance(item, dict):
                continue
            kid, algorithm, key_type = item.get("kid"), item.get("alg"), item.get("kty")
            if (
                isinstance(kid, str)
                and isinstance(algorithm, str)
                and algorithm in _ALLOWED_ALGORITHMS
                and isinstance(key_type, str)
                and key_type in {"RSA", "EC"}
            ):
                try:
                    parsed[kid] = PyJWK.from_dict(item, algorithm=algorithm)
                except (jwt.PyJWTError, KeyError, TypeError, ValueError):
                    continue
        return parsed

    @staticmethod
    def _validate_secure_url(url: str) -> None:
        parts = urlsplit(url)
        localhost = parts.hostname in {"localhost", "127.0.0.1", "::1"}
        if parts.username is not None or parts.password is not None:
            raise ValueError("OIDC URL must not contain credentials")
        if not parts.netloc or (
            parts.scheme != "https" and not (parts.scheme == "http" and localhost)
        ):
            raise ValueError("OIDC URL must use HTTPS (HTTP is allowed only for localhost)")

    def _validate_jwks_url(self, url: str) -> None:
        issuer, jwks = urlsplit(self.issuer), urlsplit(url)
        if (jwks.scheme, jwks.hostname, jwks.port) != (issuer.scheme, issuer.hostname, issuer.port):
            raise ValueError("OIDC JWKS must have the issuer origin")
        self._validate_secure_url(url)
