from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from novel_agent.db.base import Base


def _project_foreign_key(name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["tenant_id", "project_id"],
        ["projects.tenant_id", "projects.id"],
        name=name,
    )


_LOCATION_CHECK = """
char_start >= 0 AND char_end > char_start
AND ((page_start IS NULL AND page_end IS NULL)
     OR (page_start >= 1 AND page_end >= page_start))
AND ((paragraph_start IS NULL AND paragraph_end IS NULL)
     OR (paragraph_start >= 0 AND paragraph_end >= paragraph_start))
"""


class SourceDocument(Base):
    """Immutable source evidence plus a small, constrained processing state."""

    __tablename__ = "source_documents"
    __table_args__ = (
        _project_foreign_key("fk_source_docs_tenant_project"),
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_source_docs_scope_id"),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "id",
            "content_sha256",
            name="uq_source_docs_scope_id_content",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "upload_request_id",
            name="uq_source_docs_scope_request",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "content_sha256",
            name="uq_source_docs_scope_content",
        ),
        UniqueConstraint("object_key", name="uq_source_docs_object_key"),
        CheckConstraint("byte_count > 0", name="ck_source_docs_byte_count"),
        CheckConstraint(
            "length(btrim(upload_request_id)) > 0 "
            "AND length(btrim(original_display_name)) > 0 "
            "AND length(btrim(declared_media_type)) > 0 "
            "AND length(btrim(detected_media_type)) > 0 "
            "AND jsonb_typeof(authorization_provenance) = 'object'",
            name="ck_source_docs_required_text",
        ),
        CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$' AND authorization_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_source_docs_hashes",
        ),
        CheckConstraint(
            "object_key ~ '^[A-Za-z0-9][A-Za-z0-9/_-]{0,499}$'",
            name="ck_source_docs_object_key",
        ),
        CheckConstraint(
            "status IN ('upload_pending', 'uploaded', 'accepted', 'parsing', 'parsed', "
            "'chunks_ready', 'failed', 'quarantined')",
            name="ck_source_docs_status",
        ),
        CheckConstraint(
            "((status IN ('failed', 'quarantined')) = (failure_code IS NOT NULL)) "
            "AND (failure_code IS NULL OR failure_code ~ '^[a-z][a-z0-9_]{0,63}$')",
            name="ck_source_docs_failure_state",
        ),
        CheckConstraint(
            "status NOT IN ('accepted', 'parsing', 'parsed', 'chunks_ready') "
            "OR accepted_at IS NOT NULL",
            name="ck_source_docs_lifecycle_time",
        ),
        CheckConstraint(
            "(status IN ('parsing', 'parsed', 'chunks_ready') AND active_parse_run_id IS NOT NULL) "
            "OR status NOT IN ('parsing', 'parsed', 'chunks_ready')",
            name="ck_source_docs_active_parse",
        ),
        CheckConstraint(
            "aggregate_version >= 0 AND ("
            "(status IN ('upload_pending', 'uploaded') AND aggregate_version = 0) OR "
            "(status IN ('accepted', 'parsing') AND aggregate_version = 1) OR "
            "(status = 'parsed' AND aggregate_version = 2) OR "
            "(status = 'chunks_ready' AND aggregate_version >= 3) OR "
            "status IN ('failed', 'quarantined'))",
            name="ck_source_docs_aggregate_version",
        ),
        Index("ix_source_docs_scope_status", "tenant_id", "project_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id"), index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    upload_request_id: Mapped[str] = mapped_column(String(100))
    authorization_provenance: Mapped[dict[str, Any]] = mapped_column(JSONB)
    authorization_sha256: Mapped[str] = mapped_column(String(64))
    original_display_name: Mapped[str] = mapped_column(String(255))
    declared_media_type: Mapped[str] = mapped_column(String(100))
    detected_media_type: Mapped[str] = mapped_column(String(100))
    byte_count: Mapped[int] = mapped_column(BigInteger)
    content_sha256: Mapped[str] = mapped_column(String(64))
    object_key: Mapped[str] = mapped_column(String(500))
    active_parse_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    aggregate_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status: Mapped[str] = mapped_column(
        String(30), default="upload_pending", server_default="upload_pending"
    )
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SourceUploadSession(Base):
    """Short-lived authority record for one bounded, one-time upload declaration."""

    __tablename__ = "source_upload_sessions"
    __table_args__ = (
        _project_foreign_key("fk_source_upload_sessions_tenant_project"),
        UniqueConstraint(
            "tenant_id", "project_id", "request_id", name="uq_source_upload_scope_request"
        ),
        UniqueConstraint(
            "tenant_id", "project_id", "upload_id", name="uq_source_upload_scope_upload"
        ),
        CheckConstraint("byte_count > 0", name="ck_source_upload_byte_count"),
        CheckConstraint("content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_source_upload_content_hash"),
        CheckConstraint(
            "status IN ('pending', 'validating', 'consumed', 'expired')",
            name="ck_source_upload_status",
        ),
        CheckConstraint(
            "(status = 'validating' AND attempt_token IS NOT NULL "
            "AND completion_request_id IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'validating')",
            name="ck_source_upload_validation_lease",
        ),
        CheckConstraint(
            "(status = 'consumed' AND consumed_at IS NOT NULL "
            "AND completion_request_id IS NOT NULL AND attempt_token IS NULL "
            "AND lease_expires_at IS NULL) OR (status <> 'consumed' AND consumed_at IS NULL)",
            name="ck_source_upload_consumption",
        ),
        CheckConstraint(
            "length(btrim(declared_by_subject)) BETWEEN 1 AND 255 "
            "AND declared_by_subject !~ '[[:cntrl:]]'",
            name="ck_source_upload_declared_subject",
        ),
        CheckConstraint(
            "(status = 'pending' AND completion_request_id IS NULL AND attempt_token IS NULL "
            "AND lease_expires_at IS NULL AND consumed_at IS NULL) OR "
            "(status = 'validating' AND completion_request_id IS NOT NULL "
            "AND attempt_token IS NOT NULL AND lease_expires_at IS NOT NULL "
            "AND consumed_at IS NULL) OR "
            "(status = 'expired' AND attempt_token IS NULL AND lease_expires_at IS NULL "
            "AND consumed_at IS NULL) OR "
            "(status = 'consumed' AND completion_request_id IS NOT NULL "
            "AND attempt_token IS NULL AND lease_expires_at IS NULL "
            "AND consumed_at IS NOT NULL)",
            name="ck_source_upload_lifecycle_fields",
        ),
        Index("ix_source_upload_scope_status", "tenant_id", "project_id", "status", "expires_at"),
    )

    upload_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    source_document_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id"), index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    request_id: Mapped[str] = mapped_column(String(100))
    original_display_name: Mapped[str] = mapped_column(String(255))
    file_type: Mapped[str] = mapped_column(String(10))
    declared_media_type: Mapped[str] = mapped_column(String(100))
    byte_count: Mapped[int] = mapped_column(BigInteger)
    content_sha256: Mapped[str] = mapped_column(String(64))
    authorization_declaration: Mapped[dict[str, Any]] = mapped_column(JSONB)
    declared_by_subject: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default="pending")
    completion_request_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    attempt_token: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )


class SourceParseRun(Base):
    """One append-only parse attempt identified by all reproducibility inputs."""

    __tablename__ = "source_parse_runs"
    __table_args__ = (
        _project_foreign_key("fk_source_parse_runs_tenant_project"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_document_id", "source_content_sha256"],
            [
                "source_documents.tenant_id",
                "source_documents.project_id",
                "source_documents.id",
                "source_documents.content_sha256",
            ],
            name="fk_source_parse_runs_source_doc",
        ),
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_source_parse_runs_scope_id"),
        UniqueConstraint(
            "tenant_id", "project_id", "source_document_id", "id",
            name="uq_source_parse_runs_scope_source_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "source_document_id",
            "source_content_sha256",
            "parser_name",
            "parser_version",
            "parser_config_sha256",
            "chunker_version",
            "expected_chunker_name",
            "expected_chunker_config_sha256",
            "expected_containment_policy",
            "identity_schema_version",
            "expected_execution_config_sha256",
            name="uq_source_parse_identity",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "source_document_id",
            "product_version",
            name="uq_source_parse_product_version",
        ),
        CheckConstraint(
            "source_content_sha256 ~ '^[0-9a-f]{64}$' AND parser_config_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_source_parse_runs_hashes",
        ),
        CheckConstraint(
            "length(btrim(parser_name)) > 0 AND length(btrim(parser_version)) > 0 "
            "AND length(btrim(chunker_version)) > 0 "
            "AND ((expected_chunker_name IS NULL "
            "AND expected_chunker_config_sha256 IS NULL "
            "AND expected_containment_policy IS NULL) OR "
            "(length(btrim(expected_chunker_name)) > 0 "
            "AND expected_chunker_config_sha256 ~ '^[0-9a-f]{64}$' "
            "AND expected_containment_policy = 'single-section-v1')) "
            "AND (status NOT IN ('pending', 'running') "
            "OR expected_chunker_name IS NOT NULL)",
            name="ck_source_parse_runs_profile",
        ),
        CheckConstraint(
            "((identity_schema_version IS NULL "
            "AND expected_execution_config_sha256 IS NULL) OR "
            "(identity_schema_version = 1 "
            "AND expected_execution_config_sha256 ~ '^[0-9a-f]{64}$')) "
            "AND (status NOT IN ('pending', 'running') "
            "OR identity_schema_version = 1)",
            name="ck_source_parse_runs_execution_identity",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'superseded')",
            name="ck_source_parse_runs_status",
        ),
        CheckConstraint(
            "(product_version IS NULL AND chunker_name IS NULL "
            "AND chunker_config_sha256 IS NULL AND containment_policy IS NULL "
            "AND product_sha256 IS NULL) OR "
            "(product_version >= 3 AND length(btrim(chunker_name)) > 0 "
            "AND chunker_config_sha256 ~ '^[0-9a-f]{64}$' "
            "AND product_sha256 ~ '^[0-9a-f]{64}$' "
            "AND containment_policy = 'single-section-v1')",
            name="ck_source_parse_product_identity",
        ),
        CheckConstraint(
            "((status = 'failed') = (failure_code IS NOT NULL)) "
            "AND (failure_code IS NULL OR failure_code ~ '^[a-z][a-z0-9_]{0,63}$')",
            name="ck_source_parse_runs_failure_state",
        ),
        CheckConstraint(
            "(status = 'pending' AND started_at IS NULL AND completed_at IS NULL) "
            "OR (status = 'running' AND started_at IS NOT NULL AND completed_at IS NULL) "
            "OR (status IN ('succeeded', 'superseded') AND started_at IS NOT NULL "
            "AND completed_at IS NOT NULL) "
            "OR (status = 'failed' AND completed_at IS NOT NULL)",
            name="ck_source_parse_runs_lifecycle_time",
        ),
        Index("ix_source_parse_runs_scope_status", "tenant_id", "project_id", "status"),
        Index(
            "uq_source_parse_one_inflight",
            "tenant_id",
            "project_id",
            "source_document_id",
            unique=True,
            postgresql_where=text("status IN ('pending', 'running')"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id"), index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    source_document_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    source_content_sha256: Mapped[str] = mapped_column(String(64))
    parser_name: Mapped[str] = mapped_column(String(64))
    parser_version: Mapped[str] = mapped_column(String(64))
    parser_config_sha256: Mapped[str] = mapped_column(String(64))
    chunker_version: Mapped[str] = mapped_column(String(64))
    expected_chunker_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expected_chunker_config_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    expected_containment_policy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    identity_schema_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expected_execution_config_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chunker_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    chunker_config_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    containment_policy: Mapped[str | None] = mapped_column(String(64), nullable=True)
    product_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    product_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(30), default="pending", server_default="pending")
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SourceSection(Base):
    """Immutable normalized evidence section produced by one parse run."""

    __tablename__ = "source_sections"
    __table_args__ = (
        _project_foreign_key("fk_source_sections_tenant_project"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "parse_run_id"],
            ["source_parse_runs.tenant_id", "source_parse_runs.project_id", "source_parse_runs.id"],
            name="fk_source_sections_parse_run",
        ),
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_source_sections_scope_id"),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "parse_run_id",
            "section_index",
            name="uq_source_sections_parse_index",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "parse_run_id",
            "id",
            name="uq_source_sections_parse_id",
        ),
        CheckConstraint("section_index >= 0", name="ck_source_sections_index"),
        CheckConstraint(
            "length(btrim(text)) > 0 AND (heading IS NULL OR length(btrim(heading)) > 0)",
            name="ck_source_sections_text",
        ),
        CheckConstraint(_LOCATION_CHECK, name="ck_source_sections_location"),
        CheckConstraint(
            "(source_ref_kind IS NULL AND source_ref_index IS NULL "
            "AND source_ref_subindex IS NULL) OR "
            "(source_ref_kind IN ('paragraph', 'page', 'table_row') "
            "AND source_ref_index >= 0 AND "
            "((source_ref_kind = 'table_row' AND source_ref_subindex >= 0) OR "
            "(source_ref_kind IN ('paragraph', 'page') AND source_ref_subindex IS NULL)))",
            name="ck_source_sections_source_ref",
        ),
        Index("ix_source_sections_scope_parse", "tenant_id", "project_id", "parse_run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id"), index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    parse_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    section_index: Mapped[int] = mapped_column(Integer)
    heading: Mapped[str | None] = mapped_column(String(500), nullable=True)
    text: Mapped[str] = mapped_column(Text)
    char_start: Mapped[int] = mapped_column(BigInteger)
    char_end: Mapped[int] = mapped_column(BigInteger)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paragraph_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paragraph_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_ref_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source_ref_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_ref_subindex: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )


class SourceChunk(Base):
    """Immutable retrievable evidence with an ID assigned by the canonical novel chunker."""

    __tablename__ = "source_chunks"
    __table_args__ = (
        _project_foreign_key("fk_source_chunks_tenant_project"),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "parse_run_id"],
            ["source_parse_runs.tenant_id", "source_parse_runs.project_id", "source_parse_runs.id"],
            name="fk_source_chunks_parse_run",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "parse_run_id", "section_id"],
            [
                "source_sections.tenant_id",
                "source_sections.project_id",
                "source_sections.parse_run_id",
                "source_sections.id",
            ],
            name="fk_source_chunks_section",
        ),
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_source_chunks_scope_id"),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "parse_run_id",
            "section_id",
            "char_start",
            "char_end",
            name="uq_source_chunks_parse_span",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "parse_run_id",
            "chunk_index",
            name="uq_source_chunks_parse_index",
        ),
        CheckConstraint("chunk_index >= 0", name="ck_source_chunks_index"),
        CheckConstraint("token_count IS NULL OR token_count > 0", name="ck_source_chunks_tokens"),
        CheckConstraint("length(btrim(text)) > 0", name="ck_source_chunks_text"),
        CheckConstraint(_LOCATION_CHECK, name="ck_source_chunks_location"),
        CheckConstraint(
            "(source_ref_kind IS NULL AND source_ref_index IS NULL "
            "AND source_ref_subindex IS NULL) OR "
            "(source_ref_kind IN ('paragraph', 'page', 'table_row') "
            "AND source_ref_index >= 0 AND "
            "((source_ref_kind = 'table_row' AND source_ref_subindex >= 0) OR "
            "(source_ref_kind IN ('paragraph', 'page') AND source_ref_subindex IS NULL)))",
            name="ck_source_chunks_source_ref",
        ),
        Index("ix_source_chunks_scope_parse", "tenant_id", "project_id", "parse_run_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id"), index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    parse_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    section_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    chunk_index: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    char_start: Mapped[int] = mapped_column(BigInteger)
    char_end: Mapped[int] = mapped_column(BigInteger)
    page_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    page_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paragraph_start: Mapped[int | None] = mapped_column(Integer, nullable=True)
    paragraph_end: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_ref_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    source_ref_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_ref_subindex: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )


class ProjectionCheckpoint(Base):
    """Authoritative progress for a rebuildable external projection."""

    __tablename__ = "projection_checkpoints"
    __table_args__ = (
        _project_foreign_key("fk_projection_ckpts_tenant_project"),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "projection_name",
            "checkpoint_key",
            name="uq_projection_ckpts_scope_name_key",
        ),
        CheckConstraint("projection_version >= 0", name="ck_projection_ckpts_version"),
        CheckConstraint(
            "attempted_version >= projection_version",
            name="ck_projection_ckpts_attempted_version",
        ),
        CheckConstraint(
            "((projection_version = 0 AND last_applied_event_id IS NULL "
            "AND last_applied_event_occurred_at IS NULL) "
            "OR (projection_version > 0 AND last_applied_event_id IS NOT NULL "
            "AND last_applied_event_occurred_at IS NOT NULL)) "
            "AND ((attempted_version = 0 AND last_attempted_event_id IS NULL) "
            "OR (attempted_version > 0 AND last_attempted_event_id IS NOT NULL))",
            name="ck_projection_ckpts_event_identity",
        ),
        CheckConstraint(
            "length(btrim(projection_name)) > 0 AND length(btrim(checkpoint_key)) > 0",
            name="ck_projection_ckpts_required_text",
        ),
        CheckConstraint(
            "status IN ('current', 'lagging', 'failed', 'rebuilding')",
            name="ck_projection_ckpts_status",
        ),
        CheckConstraint(
            "((status = 'failed') = (failure_code IS NOT NULL)) "
            "AND (failure_code IS NULL OR failure_code ~ '^[a-z][a-z0-9_]{0,63}$')",
            name="ck_projection_ckpts_failure_state",
        ),
        CheckConstraint(
            "(source_document_id IS NULL AND parse_run_id IS NULL "
            "AND product_sha256 IS NULL AND embedding_identity_sha256 IS NULL "
            "AND vector_schema_version IS NULL) OR "
            "(source_document_id IS NOT NULL AND parse_run_id IS NOT NULL "
            "AND product_sha256 IS NOT NULL AND product_sha256 ~ '^[0-9a-f]{64}$' "
            "AND embedding_identity_sha256 IS NOT NULL "
            "AND embedding_identity_sha256 ~ '^[0-9a-f]{64}$' "
            "AND vector_schema_version IS NOT NULL AND vector_schema_version > 0)",
            name="ck_projection_ckpts_lineage",
        ),
        CheckConstraint(
            "attempt_count >= 0", name="ck_projection_ckpts_attempt_count"
        ),
        Index("ix_projection_ckpts_scope_status", "tenant_id", "project_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id"), index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    projection_name: Mapped[str] = mapped_column(String(64))
    checkpoint_key: Mapped[str] = mapped_column(String(200))
    projection_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    status: Mapped[str] = mapped_column(String(30), default="lagging", server_default="lagging")
    last_applied_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    last_applied_event_occurred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempted_version: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_attempted_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    failure_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_document_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    parse_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    product_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embedding_identity_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    vector_schema_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    attempt_token: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    lag_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_succeeded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        server_default=func.now(),
        onupdate=func.now(),
    )
