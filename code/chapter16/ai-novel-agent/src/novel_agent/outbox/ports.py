from __future__ import annotations

import uuid
from collections.abc import Iterator, Mapping
from datetime import datetime
from math import isfinite
from types import MappingProxyType
from typing import Any, Protocol, cast

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator


class FrozenDict(Mapping[str, Any]):
    """Read-only mapping used recursively for projection event snapshots."""

    def __init__(self, values: Mapping[str, Any]) -> None:
        self._values = MappingProxyType(dict(values))

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return FrozenDict({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _validate_json_tree(value: Any) -> None:
    if value is None or isinstance(value, (bool, int, str)):
        return
    if isinstance(value, float):
        if not isfinite(value):
            raise ValueError("outbox payload numbers must be finite")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("outbox payload keys must be strings")
            _validate_json_tree(item)
        return
    if isinstance(value, (list, tuple)):
        for item in value:
            _validate_json_tree(item)
        return
    raise ValueError("outbox payload must contain only JSON values")


def _serializable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _serializable(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_serializable(item) for item in value]
    return value


class OutboxEnvelope(BaseModel):
    """Storage-neutral event passed from the authority outbox to projections."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    event_id: uuid.UUID
    tenant_id: uuid.UUID
    project_id: uuid.UUID
    aggregate_type: str
    aggregate_id: uuid.UUID
    aggregate_version: int
    event_type: str
    payload: Mapping[str, Any]
    occurred_at: datetime

    @field_validator("payload", mode="before")
    @classmethod
    def validate_payload_tree(cls, value: Any) -> Any:
        _validate_json_tree(value)
        return value

    @field_validator("payload", mode="after")
    @classmethod
    def freeze_payload(cls, value: Mapping[str, Any]) -> Mapping[str, Any]:
        return cast(Mapping[str, Any], _freeze(value))

    @field_serializer("payload")
    def serialize_payload(self, value: Mapping[str, Any]) -> dict[str, Any]:
        serialized = _serializable(value)
        if not isinstance(serialized, dict):
            raise TypeError("outbox payload must be a mapping")
        return serialized


class GraphProjection(Protocol):
    async def apply(self, event: OutboxEnvelope) -> None: ...

    async def delete_project(self, tenant_id: uuid.UUID, project_id: uuid.UUID) -> None: ...


class VectorProjection(Protocol):
    async def apply(self, event: OutboxEnvelope) -> None: ...

    async def delete_project(self, tenant_id: uuid.UUID, project_id: uuid.UUID) -> None: ...
