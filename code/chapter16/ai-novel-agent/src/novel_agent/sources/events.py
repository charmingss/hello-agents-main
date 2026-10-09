from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from novel_agent.sources.contracts import BoundedToken, RequestId, Sha256Hex, SourceMediaType

FailureCode = Annotated[
    str,
    StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$"),
]
FailedStage = Literal["acceptance", "storage", "parsing", "chunking", "projection"]


class _SourceEvent(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    event_id: UUID
    tenant_id: UUID
    project_id: UUID
    aggregate_type: Literal["source"] = "source"
    aggregate_id: UUID
    source_document_id: UUID
    request_id: RequestId
    correlation_id: UUID
    causation_id: UUID | None
    occurred_at: datetime
    schema_version: Literal[1] = 1

    @field_validator("occurred_at")
    @classmethod
    def require_aware_time(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("occurred_at must include a timezone")
        return value

    @model_validator(mode="after")
    def require_matching_aggregate(self) -> _SourceEvent:
        if self.aggregate_id != self.source_document_id:
            raise ValueError("source aggregate identity must match source_document_id")
        return self


class SourceAcceptedV1(_SourceEvent):
    event_type: Literal["source.accepted.v1"] = "source.accepted.v1"
    aggregate_version: Literal[1] = 1
    content_sha256: Sha256Hex
    media_type: SourceMediaType
    byte_count: Annotated[int, Field(gt=0)]


class SourceParsedV1(_SourceEvent):
    event_type: Literal["source.parsed.v1"] = "source.parsed.v1"
    aggregate_version: Literal[2] = 2
    parse_run_id: UUID
    parser_name: BoundedToken
    parser_version: BoundedToken
    parser_config_sha256: Sha256Hex
    chunker_version: BoundedToken
    section_count: Annotated[int, Field(ge=0)]
    extracted_char_count: Annotated[int, Field(ge=0)]


class SourceChunksReadyV1(_SourceEvent):
    event_type: Literal["source.chunks.ready.v1"] = "source.chunks.ready.v1"
    aggregate_version: Annotated[int, Field(ge=3)] = 3
    parse_run_id: UUID
    parser_name: BoundedToken
    parser_version: BoundedToken
    parser_config_sha256: Sha256Hex
    chunker_name: BoundedToken
    chunker_version: BoundedToken
    chunker_config_sha256: Sha256Hex
    product_sha256: Sha256Hex
    containment_policy: Literal["single-section-v1"]
    section_count: Annotated[int, Field(gt=0)]
    chunk_count: Annotated[int, Field(gt=0)]


class SourceFailedV1(_SourceEvent):
    event_type: Literal["source.failed.v1"] = "source.failed.v1"
    aggregate_version: Literal[2, 3] = 2
    parse_run_id: UUID | None = None
    failure_code: FailureCode
    failed_stage: FailedStage


SourceIntegrationEvent = SourceAcceptedV1 | SourceParsedV1 | SourceChunksReadyV1 | SourceFailedV1

__all__ = [
    "SourceAcceptedV1",
    "SourceChunksReadyV1",
    "SourceFailedV1",
    "FailedStage",
    "SourceIntegrationEvent",
    "SourceParsedV1",
]
