from fastapi import Header, Request

from novel_agent.api.errors import InvalidTokenError, invalid_token_http_exception
from novel_agent.auth.contracts import PrincipalVerifier
from novel_agent.auth.models import Principal


def get_oidc_verifier(request: Request) -> PrincipalVerifier:
    try:
        verifier: PrincipalVerifier = request.app.state.oidc_verifier
    except AttributeError:
        raise RuntimeError(
            "OIDC verifier is not configured; application lifespan did not run"
        ) from None
    return verifier


async def get_principal(
    request: Request,
    authorization: str | None = Header(default=None),
) -> Principal:
    if authorization is None:
        raise invalid_token_http_exception()
    parts = authorization.split()
    if len(parts) != 2 or parts[0].lower() != "bearer" or not parts[1]:
        raise invalid_token_http_exception()
    try:
        return await get_oidc_verifier(request).verify(parts[1])
    except InvalidTokenError:
        raise invalid_token_http_exception() from None
