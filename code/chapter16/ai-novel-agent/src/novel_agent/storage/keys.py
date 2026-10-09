from __future__ import annotations

import hashlib
import re
from enum import StrEnum
from typing import ClassVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, field_validator

_TOKEN = r"[0-9a-f]{32}"
_SHA = r"[0-9a-f]{64}"


class ObjectKind(StrEnum):
    STAGING = "staging"
    IMMUTABLE = "immutable"
    QUARANTINE = "quarantine"


class StorageScope(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    tenant_id: UUID
    project_id: UUID

    @property
    def tenant_token(self) -> str:
        return _token("tenant", self.tenant_id)

    @property
    def project_token(self) -> str:
        return _token("project", self.project_id)


def _token(domain: str, identifier: UUID) -> str:
    material = f"novel-agent-storage-v1:{domain}:{identifier.hex}".encode()
    return hashlib.sha256(material).hexdigest()[:32]


class ScopedObjectKey(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")
    value: str

    expected_kind: ClassVar[ObjectKind] = ObjectKind.STAGING

    @classmethod
    def _pattern(cls) -> re.Pattern[str]:
        raise NotImplementedError

    @field_validator("value")
    @classmethod
    def validate_key(cls, value: str) -> str:
        if value != value.casefold() or "%" in value or "\\" in value:
            raise ValueError("object key is not canonical")
        if cls._pattern().fullmatch(value) is None:
            raise ValueError("object key has the wrong kind or shape")
        return value

    @property
    def parts(self) -> tuple[str, ...]:
        return tuple(self.value.split("/"))

    @property
    def kind(self) -> ObjectKind:
        return ObjectKind(self.parts[1])

    @property
    def tenant_token(self) -> str:
        return self.parts[2]

    @property
    def project_token(self) -> str:
        return self.parts[3]

    @property
    def source_token(self) -> str | None:
        return None

    def belongs_to(self, scope: StorageScope) -> bool:
        return self.tenant_token == scope.tenant_token and self.project_token == scope.project_token

    def __str__(self) -> str:
        return self.value


class StagingObjectKey(ScopedObjectKey):
    expected_kind: ClassVar[ObjectKind] = ObjectKind.STAGING

    @classmethod
    def _pattern(cls) -> re.Pattern[str]:
        return re.compile(rf"^v1/staging/{_TOKEN}/{_TOKEN}/{_TOKEN}/{_TOKEN}$")

    @property
    def source_token(self) -> str:
        return self.parts[4]


class ImmutableObjectKey(ScopedObjectKey):
    expected_kind: ClassVar[ObjectKind] = ObjectKind.IMMUTABLE

    @classmethod
    def _pattern(cls) -> re.Pattern[str]:
        return re.compile(rf"^v1/immutable/{_TOKEN}/{_TOKEN}/{_TOKEN}/{_SHA}$")

    @property
    def source_token(self) -> str:
        return self.parts[4]


class QuarantineObjectKey(ScopedObjectKey):
    expected_kind: ClassVar[ObjectKind] = ObjectKind.QUARANTINE

    @classmethod
    def _pattern(cls) -> re.Pattern[str]:
        return re.compile(rf"^v1/quarantine/{_TOKEN}/{_TOKEN}/{_TOKEN}/{_TOKEN}$")

    @property
    def source_token(self) -> str:
        return self.parts[4]


class ObjectKeyFactory:
    def staging(self, *, scope: StorageScope, source_id: UUID, upload_id: UUID) -> StagingObjectKey:
        return StagingObjectKey(
            value=(
                f"v1/staging/{scope.tenant_token}/{scope.project_token}/"
                f"{_token('source', source_id)}/{_token('upload', upload_id)}"
            )
        )

    def immutable(self, *, scope: StorageScope, source_id: UUID, sha256: str) -> ImmutableObjectKey:
        if re.fullmatch(_SHA, sha256) is None:
            raise ValueError("sha256 must be lowercase hexadecimal")
        return ImmutableObjectKey(
            value=(
                f"v1/immutable/{scope.tenant_token}/{scope.project_token}/"
                f"{_token('source', source_id)}/{sha256}"
            )
        )

    def quarantine(
        self, *, scope: StorageScope, source_id: UUID, incident_id: UUID
    ) -> QuarantineObjectKey:
        return QuarantineObjectKey(
            value=(
                f"v1/quarantine/{scope.tenant_token}/{scope.project_token}/"
                f"{_token('source', source_id)}/{_token('incident', incident_id)}"
            )
        )


__all__ = [
    "ImmutableObjectKey",
    "ObjectKeyFactory",
    "ObjectKind",
    "QuarantineObjectKey",
    "ScopedObjectKey",
    "StagingObjectKey",
    "StorageScope",
]
