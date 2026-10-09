from __future__ import annotations

import hashlib
import json
import re
import uuid
from math import isfinite
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from novel_agent.embeddings.ports import EmbeddingDistance, EmbeddingIdentity
from novel_agent.sources.contracts import BoundedToken, Sha256Hex

VECTOR_SCHEMA_VERSION = 1
POINT_ID_NAMESPACE = uuid.UUID("385af5d9-47a7-47f8-a029-2d37dc4ca5fb")
COLLECTION_ALIAS = "novel_chunks_current"

CitationText = Annotated[str, StringConstraints(min_length=1, max_length=500)]
ChunkText = Annotated[str, StringConstraints(min_length=1, max_length=1_000_000)]
SourceRefKind = Literal["paragraph", "table_row", "page"]
MemoryKind = Literal["source_chunk"]
ProjectionStatus = Literal["active", "superseded"]


class TenantProjectScope(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    tenant_id: uuid.UUID
    project_id: uuid.UUID


class ChunkVectorPayload(BaseModel):
    """Self-describing projection payload; PostgreSQL remains its authority."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    tenant_id: uuid.UUID
    project_id: uuid.UUID
    source_document_id: uuid.UUID
    parse_run_id: uuid.UUID
    product_version: Annotated[int, Field(ge=3)]
    product_sha256: Sha256Hex
    section_id: uuid.UUID
    chunk_id: uuid.UUID
    memory_kind: MemoryKind
    authority: Literal["postgresql"]
    status: ProjectionStatus
    parser_name: BoundedToken
    parser_version: BoundedToken
    parser_config_sha256: Sha256Hex
    chunker_name: BoundedToken
    chunker_version: BoundedToken
    chunker_config_sha256: Sha256Hex
    containment_policy: Literal["single-section-v1"]
    embedding_provider: BoundedToken
    embedding_model: Annotated[str, StringConstraints(min_length=1, max_length=200)]
    embedding_version: BoundedToken
    embedding_dimension: Annotated[int, Field(gt=0, le=1_000_000)]
    embedding_config_sha256: Sha256Hex
    embedding_distance: EmbeddingDistance
    source_ref_kind: SourceRefKind
    source_ref_index: Annotated[int, Field(ge=0)]
    source_ref_subindex: Annotated[int, Field(ge=0)] | None
    char_start: Annotated[int, Field(ge=0)]
    char_end: Annotated[int, Field(gt=0)]
    citation_label: CitationText
    citation_heading: CitationText | None
    text: ChunkText

    @model_validator(mode="after")
    def validate_span(self) -> ChunkVectorPayload:
        if self.char_end <= self.char_start:
            raise ValueError("char_end must be greater than char_start")
        if self.source_ref_kind == "table_row" and self.source_ref_subindex is None:
            raise ValueError("table_row source_ref_subindex is required")
        if self.source_ref_kind != "table_row" and self.source_ref_subindex is not None:
            raise ValueError("source_ref_subindex is only valid for table_row")
        return self

    def matches_embedding(self, identity: EmbeddingIdentity) -> bool:
        return (
            self.embedding_provider == identity.provider
            and self.embedding_model == identity.model
            and self.embedding_version == identity.version
            and self.embedding_dimension == identity.dimension
            and self.embedding_config_sha256 == identity.configuration_sha256
            and self.embedding_distance == identity.distance
        )


class VectorPoint(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    id: uuid.UUID
    vector: tuple[float, ...]
    payload: ChunkVectorPayload

    @classmethod
    def from_payload(
        cls,
        payload: ChunkVectorPayload,
        vector: tuple[float, ...],
        identity: EmbeddingIdentity,
    ) -> VectorPoint:
        if not payload.matches_embedding(identity):
            raise ValueError("payload embedding identity mismatch")
        _validate_vector(vector, identity.dimension)
        return cls(
            id=deterministic_point_id(payload, identity, schema_version=VECTOR_SCHEMA_VERSION),
            vector=vector,
            payload=payload,
        )


class ProjectionFilter(BaseModel):
    """Allow-listed filters only; arbitrary Qdrant filter trees are intentionally absent."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    source_document_id: uuid.UUID | None = None
    parse_run_id: uuid.UUID | None = None
    memory_kind: MemoryKind | None = None
    status: ProjectionStatus | None = None


class SourceCitation(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    source_document_id: uuid.UUID
    parse_run_id: uuid.UUID
    product_version: int
    product_sha256: Sha256Hex
    section_id: uuid.UUID
    chunk_id: uuid.UUID
    citation_label: CitationText
    citation_heading: CitationText | None
    source_ref_kind: SourceRefKind
    source_ref_index: int
    source_ref_subindex: int | None
    char_start: int
    char_end: int
    parser_name: BoundedToken | None = None
    parser_version: BoundedToken | None = None
    parser_config_sha256: Sha256Hex | None = None
    chunker_name: BoundedToken | None = None
    chunker_version: BoundedToken | None = None
    chunker_config_sha256: Sha256Hex | None = None
    containment_policy: Literal["single-section-v1"] | None = None


class VectorSearchHit(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    point_id: uuid.UUID
    score: float
    text: ChunkText
    citation: SourceCitation

    @model_validator(mode="after")
    def validate_score(self) -> VectorSearchHit:
        if not isfinite(self.score):
            raise ValueError("score must be finite")
        return self


def _identity_material(identity: EmbeddingIdentity, schema_version: int) -> str:
    return json.dumps(
        {"embedding": identity.model_dump(mode="json"), "schema_version": schema_version},
        sort_keys=True,
        separators=(",", ":"),
    )


def physical_collection_name(
    identity: EmbeddingIdentity, *, schema_version: int = VECTOR_SCHEMA_VERSION
) -> str:
    if schema_version <= 0:
        raise ValueError("schema_version must be positive")
    slug = re.sub(r"[^a-z0-9]+", "_", f"{identity.provider}_{identity.model}".lower()).strip("_")[
        :30
    ]
    digest = hashlib.sha256(_identity_material(identity, schema_version).encode()).hexdigest()[:24]
    return (
        f"novel_chunks_v{schema_version}_{slug}_{identity.dimension}_{identity.distance}_{digest}"
    )


def collection_alias_name(
    identity: EmbeddingIdentity,
    *,
    logical_alias: str = COLLECTION_ALIAS,
    schema_version: int = VECTOR_SCHEMA_VERSION,
) -> str:
    """Bind a logical alias to one complete, schema-versioned embedding identity."""

    if schema_version <= 0:
        raise ValueError("schema_version must be positive")
    slug = re.sub(r"[^a-z0-9_-]+", "_", logical_alias.strip().lower()).strip("_-")[:70]
    if not slug:
        raise ValueError("logical alias must contain a letter or digit")
    digest = hashlib.sha256(_identity_material(identity, schema_version).encode()).hexdigest()[:24]
    return f"{slug}_{digest}"


def deterministic_point_id(
    payload: ChunkVectorPayload,
    identity: EmbeddingIdentity,
    *,
    schema_version: int,
) -> uuid.UUID:
    if schema_version <= 0:
        raise ValueError("schema_version must be positive")
    if not payload.matches_embedding(identity):
        raise ValueError("payload embedding identity mismatch")
    material = {
        "tenant_id": str(payload.tenant_id),
        "project_id": str(payload.project_id),
        "source_document_id": str(payload.source_document_id),
        "parse_run_id": str(payload.parse_run_id),
        "product_version": payload.product_version,
        "product_sha256": payload.product_sha256,
        "chunk_id": str(payload.chunk_id),
        "embedding": identity.model_dump(mode="json"),
        "schema_version": schema_version,
    }
    return uuid.uuid5(
        POINT_ID_NAMESPACE,
        json.dumps(material, sort_keys=True, separators=(",", ":")),
    )


def _validate_vector(vector: tuple[float, ...], dimension: int) -> None:
    if len(vector) != dimension:
        raise ValueError("vector dimension mismatch")
    if any(not isfinite(component) for component in vector):
        raise ValueError("vector components must be finite")


__all__ = [
    "COLLECTION_ALIAS",
    "VECTOR_SCHEMA_VERSION",
    "ChunkVectorPayload",
    "ProjectionFilter",
    "SourceCitation",
    "TenantProjectScope",
    "VectorPoint",
    "VectorSearchHit",
    "collection_alias_name",
    "deterministic_point_id",
    "physical_collection_name",
]
