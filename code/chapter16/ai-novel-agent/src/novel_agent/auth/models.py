from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class OidcClaims(BaseModel):
    """The verified subset of claims needed by the application.

    Providers commonly add private claims, so unknown JWT claims are intentionally ignored.
    """

    model_config = ConfigDict(frozen=True, extra="ignore")

    subject: str = Field(alias="sub", min_length=1)
    tenant_id: UUID
    audience: str | list[str] = Field(alias="aud")
    issuer: str = Field(alias="iss", min_length=1)
    expires_at: int | None = Field(default=None, alias="exp")
    roles: frozenset[str] = frozenset()


class Principal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str = Field(min_length=1)
    tenant_id: UUID
    roles: frozenset[str] = frozenset()
