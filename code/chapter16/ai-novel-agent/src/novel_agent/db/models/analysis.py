"""Original model evidence and append-only human review history."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from novel_agent.db.base import Base


class SourceAnalysisRecord(Base):
    __tablename__ = "source_analysis_records"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_document_id"],
            ["source_documents.tenant_id", "source_documents.project_id", "source_documents.id"],
            name="fk_analysis_records_source",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_document_id", "parse_run_id"],
            ["source_parse_runs.tenant_id", "source_parse_runs.project_id",
             "source_parse_runs.source_document_id", "source_parse_runs.id"],
            name="fk_analysis_records_parse",
        ),
        UniqueConstraint("tenant_id", "project_id", "source_document_id", "id",
                         name="uq_analysis_records_scope_id"),
        CheckConstraint("product_version >= 3 AND current_revision >= 0",
                        name="ck_analysis_records_versions"),
        CheckConstraint("product_sha256 ~ '^[0-9a-f]{64}$'", name="ck_analysis_records_digest"),
        CheckConstraint(
            "length(btrim(provider)) > 0 AND length(btrim(model)) > 0 "
            "AND length(btrim(prompt_version)) > 0 AND length(btrim(created_by_subject)) > 0",
            name="ck_analysis_records_text",
        ),
        CheckConstraint(
            "jsonb_typeof(original) = 'object' "
            "AND COALESCE(jsonb_typeof(original->'characters'), '') = 'array' "
            "AND COALESCE(jsonb_typeof(original->'summary'), '') = 'array' "
            "AND jsonb_typeof(character_ids) = 'array' "
            "AND jsonb_array_length(character_ids) = jsonb_array_length(original->'characters') "
            "AND jsonb_array_length(character_ids) <= 500",
            name="ck_analysis_records_json",
        ),
        Index("ix_analysis_records_history", "tenant_id", "project_id", "source_document_id",
              "created_at", "id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    source_document_id: Mapped[UUID] = mapped_column(Uuid)
    parse_run_id: Mapped[UUID] = mapped_column(Uuid)
    product_version: Mapped[int] = mapped_column(Integer)
    product_sha256: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(100))
    original: Mapped[dict[str, Any]] = mapped_column(JSONB)
    character_ids: Mapped[list[str]] = mapped_column(JSONB)
    current_revision: Mapped[int] = mapped_column(Integer)
    created_by_subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class SourceAnalysisReview(Base):
    __tablename__ = "source_analysis_reviews"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_document_id", "analysis_id"],
            ["source_analysis_records.tenant_id", "source_analysis_records.project_id",
             "source_analysis_records.source_document_id", "source_analysis_records.id"],
            name="fk_analysis_reviews_record",
        ),
        UniqueConstraint("tenant_id", "project_id", "source_document_id", "analysis_id", "revision",
                         name="uq_analysis_reviews_scope_revision"),
        CheckConstraint("revision >= 0", name="ck_analysis_reviews_revision"),
        CheckConstraint("length(btrim(subject)) > 0", name="ck_analysis_reviews_subject"),
        CheckConstraint(
            "jsonb_typeof(snapshot) = 'object' "
            "AND COALESCE(jsonb_typeof(snapshot->'characters'), '') = 'array' "
            "AND jsonb_array_length(snapshot->'characters') <= 500",
            name="ck_analysis_reviews_json",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    source_document_id: Mapped[UUID] = mapped_column(Uuid)
    analysis_id: Mapped[UUID] = mapped_column(Uuid)
    revision: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FullAnalysisJob(Base):
    """One source-version-pinned, resumable full-document analysis job."""

    __tablename__ = "source_analysis_jobs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_full_analysis_jobs_project",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_document_id"],
            ["source_documents.tenant_id", "source_documents.project_id", "source_documents.id"],
            name="fk_full_analysis_jobs_source",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_document_id", "parse_run_id"],
            [
                "source_parse_runs.tenant_id",
                "source_parse_runs.project_id",
                "source_parse_runs.source_document_id",
                "source_parse_runs.id",
            ],
            name="fk_full_analysis_jobs_parse",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_document_id", "final_analysis_id"],
            [
                "source_analysis_records.tenant_id",
                "source_analysis_records.project_id",
                "source_analysis_records.source_document_id",
                "source_analysis_records.id",
            ],
            name="fk_full_analysis_jobs_final_analysis",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "source_document_id",
            "id",
            name="uq_full_analysis_jobs_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "source_document_id",
            "id",
            "provider",
            "model",
            "prompt_version",
            name="uq_full_analysis_jobs_scope_execution",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'stale')",
            name="ck_full_analysis_jobs_status",
        ),
        CheckConstraint(
            "total_batches > 0 AND completed_batches >= 0 AND failed_batches >= 0 "
            "AND completed_batches <= total_batches AND failed_batches <= total_batches "
            "AND completed_batches + failed_batches <= total_batches "
            "AND (status <> 'succeeded' OR (completed_batches = total_batches "
            "AND failed_batches = 0))",
            name="ck_full_analysis_jobs_counts",
        ),
        CheckConstraint(
            "product_version >= 3 AND product_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_full_analysis_jobs_digest",
        ),
        CheckConstraint(
            "length(btrim(provider)) > 0 AND length(btrim(model)) > 0 "
            "AND length(btrim(prompt_version)) > 0 "
            "AND length(btrim(created_by_subject)) > 0 "
            "AND (workflow_id IS NULL OR length(btrim(workflow_id)) > 0) "
            "AND (error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,63}$')",
            name="ck_full_analysis_jobs_text",
        ),
        CheckConstraint(
            "(status = 'succeeded') = (final_analysis_id IS NOT NULL)",
            name="ck_full_analysis_jobs_result",
        ),
        Index(
            "ix_full_analysis_jobs_history",
            "tenant_id",
            "project_id",
            "source_document_id",
            "created_at",
            "id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    source_document_id: Mapped[UUID] = mapped_column(Uuid)
    parse_run_id: Mapped[UUID] = mapped_column(Uuid)
    product_version: Mapped[int] = mapped_column(Integer)
    product_sha256: Mapped[str] = mapped_column(String(64))
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20))
    total_batches: Mapped[int] = mapped_column(Integer)
    completed_batches: Mapped[int] = mapped_column(Integer)
    failed_batches: Mapped[int] = mapped_column(Integer)
    workflow_id: Mapped[str | None] = mapped_column(String(255))
    final_analysis_id: Mapped[UUID | None] = mapped_column(Uuid)
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_by_subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class FullAnalysisBatch(Base):
    """One deterministic batch plan and its fenced execution result."""

    __tablename__ = "source_analysis_batches"
    __table_args__ = (
        ForeignKeyConstraint(
            [
                "tenant_id",
                "project_id",
                "source_document_id",
                "job_id",
                "provider",
                "model",
                "prompt_version",
            ],
            [
                "source_analysis_jobs.tenant_id",
                "source_analysis_jobs.project_id",
                "source_analysis_jobs.source_document_id",
                "source_analysis_jobs.id",
                "source_analysis_jobs.provider",
                "source_analysis_jobs.model",
                "source_analysis_jobs.prompt_version",
            ],
            name="fk_full_analysis_batches_job",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "source_document_id",
            "job_id",
            "batch_index",
            name="uq_full_analysis_batches_scope_index",
        ),
        CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_full_analysis_batches_status",
        ),
        CheckConstraint(
            "batch_index >= 0 AND core_start >= 0 AND core_end > core_start "
            "AND context_char_count >= 0",
            name="ck_full_analysis_batches_bounds",
        ),
        CheckConstraint(
            "jsonb_typeof(plan) = 'object' "
            "AND COALESCE(jsonb_typeof(plan->'slices'), '') = 'array' "
            "AND jsonb_array_length(plan->'slices') BETWEEN 1 AND 400",
            name="ck_full_analysis_batches_plan",
        ),
        CheckConstraint(
            "((status = 'succeeded') = (result IS NOT NULL)) "
            "AND (result IS NULL OR (jsonb_typeof(result) = 'object' "
            "AND COALESCE(jsonb_typeof(result->'characters'), '') = 'array' "
            "AND COALESCE(jsonb_typeof(result->'summary'), '') = 'array'))",
            name="ck_full_analysis_batches_result",
        ),
        CheckConstraint(
            "attempts >= 0 "
            "AND ((status = 'pending' AND attempt_token IS NULL) "
            "OR (status <> 'pending' AND attempts > 0 AND attempt_token IS NOT NULL))",
            name="ck_full_analysis_batches_attempt",
        ),
        CheckConstraint(
            "length(btrim(provider)) > 0 AND length(btrim(model)) > 0 "
            "AND length(btrim(prompt_version)) > 0 "
            "AND (error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,63}$')",
            name="ck_full_analysis_batches_text",
        ),
        Index(
            "ix_full_analysis_batches_status",
            "tenant_id",
            "project_id",
            "source_document_id",
            "job_id",
            "status",
            "batch_index",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    source_document_id: Mapped[UUID] = mapped_column(Uuid)
    job_id: Mapped[UUID] = mapped_column(Uuid)
    batch_index: Mapped[int] = mapped_column(Integer)
    core_start: Mapped[int] = mapped_column(Integer)
    core_end: Mapped[int] = mapped_column(Integer)
    context_char_count: Mapped[int] = mapped_column(Integer)
    plan: Mapped[dict[str, Any]] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(20))
    attempt_token: Mapped[UUID | None] = mapped_column(Uuid)
    attempts: Mapped[int] = mapped_column(Integer)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    provider: Mapped[str] = mapped_column(String(100))
    model: Mapped[str] = mapped_column(String(200))
    prompt_version: Mapped[str] = mapped_column(String(100))
    error_code: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
