"""Authentication primitives for verified OIDC identities."""

from novel_agent.auth.models import OidcClaims, Principal
from novel_agent.auth.verifier import OidcTokenVerifier

__all__ = ["OidcClaims", "OidcTokenVerifier", "Principal"]
