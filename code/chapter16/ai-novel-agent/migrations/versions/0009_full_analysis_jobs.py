"""Add resumable full-source analysis jobs and deterministic batches."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_full_analysis_jobs"
down_revision = "0008_analysis_history"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_analysis_records_json", "source_analysis_records", type_="check"
    )
    op.create_check_constraint(
        "ck_analysis_records_json",
        "source_analysis_records",
        "jsonb_typeof(original) = 'object' "
        "AND COALESCE(jsonb_typeof(original->'characters'), '') = 'array' "
        "AND COALESCE(jsonb_typeof(original->'summary'), '') = 'array' "
        "AND jsonb_typeof(character_ids) = 'array' "
        "AND jsonb_array_length(character_ids) = jsonb_array_length(original->'characters') "
        "AND jsonb_array_length(character_ids) <= 500",
    )
    op.drop_constraint(
        "ck_analysis_reviews_json", "source_analysis_reviews", type_="check"
    )
    op.create_check_constraint(
        "ck_analysis_reviews_json",
        "source_analysis_reviews",
        "jsonb_typeof(snapshot) = 'object' "
        "AND COALESCE(jsonb_typeof(snapshot->'characters'), '') = 'array' "
        "AND jsonb_array_length(snapshot->'characters') <= 500",
    )
    op.create_table(
        "source_analysis_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("source_document_id", sa.Uuid(), nullable=False),
        sa.Column("parse_run_id", sa.Uuid(), nullable=False),
        sa.Column("product_version", sa.Integer(), nullable=False),
        sa.Column("product_sha256", sa.String(64), nullable=False),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("total_batches", sa.Integer(), nullable=False),
        sa.Column("completed_batches", sa.Integer(), nullable=False),
        sa.Column("failed_batches", sa.Integer(), nullable=False),
        sa.Column("workflow_id", sa.String(255), nullable=True),
        sa.Column("final_analysis_id", sa.Uuid(), nullable=True),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_by_subject", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_full_analysis_jobs_project",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_document_id"],
            ["source_documents.tenant_id", "source_documents.project_id", "source_documents.id"],
            name="fk_full_analysis_jobs_source",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_document_id", "parse_run_id"],
            [
                "source_parse_runs.tenant_id",
                "source_parse_runs.project_id",
                "source_parse_runs.source_document_id",
                "source_parse_runs.id",
            ],
            name="fk_full_analysis_jobs_parse",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_document_id", "final_analysis_id"],
            [
                "source_analysis_records.tenant_id",
                "source_analysis_records.project_id",
                "source_analysis_records.source_document_id",
                "source_analysis_records.id",
            ],
            name="fk_full_analysis_jobs_final_analysis",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "source_document_id",
            "id",
            name="uq_full_analysis_jobs_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "source_document_id",
            "id",
            "provider",
            "model",
            "prompt_version",
            name="uq_full_analysis_jobs_scope_execution",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'stale')",
            name="ck_full_analysis_jobs_status",
        ),
        sa.CheckConstraint(
            "total_batches > 0 AND completed_batches >= 0 AND failed_batches >= 0 "
            "AND completed_batches <= total_batches AND failed_batches <= total_batches "
            "AND completed_batches + failed_batches <= total_batches "
            "AND (status <> 'succeeded' OR (completed_batches = total_batches "
            "AND failed_batches = 0))",
            name="ck_full_analysis_jobs_counts",
        ),
        sa.CheckConstraint(
            "product_version >= 3 AND product_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_full_analysis_jobs_digest",
        ),
        sa.CheckConstraint(
            "length(btrim(provider)) > 0 AND length(btrim(model)) > 0 "
            "AND length(btrim(prompt_version)) > 0 "
            "AND length(btrim(created_by_subject)) > 0 "
            "AND (workflow_id IS NULL OR length(btrim(workflow_id)) > 0) "
            "AND (error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,63}$')",
            name="ck_full_analysis_jobs_text",
        ),
        sa.CheckConstraint(
            "(status = 'succeeded') = (final_analysis_id IS NOT NULL)",
            name="ck_full_analysis_jobs_result",
        ),
    )
    op.create_index(
        "ix_full_analysis_jobs_history",
        "source_analysis_jobs",
        ["tenant_id", "project_id", "source_document_id", "created_at", "id"],
    )
    op.create_table(
        "source_analysis_batches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("source_document_id", sa.Uuid(), nullable=False),
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("batch_index", sa.Integer(), nullable=False),
        sa.Column("core_start", sa.Integer(), nullable=False),
        sa.Column("core_end", sa.Integer(), nullable=False),
        sa.Column("context_char_count", sa.Integer(), nullable=False),
        sa.Column("plan", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("attempt_token", sa.Uuid(), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("result", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("provider", sa.String(100), nullable=False),
        sa.Column("model", sa.String(200), nullable=False),
        sa.Column("prompt_version", sa.String(100), nullable=False),
        sa.Column("error_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
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
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "source_document_id",
            "job_id",
            "batch_index",
            name="uq_full_analysis_batches_scope_index",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed')",
            name="ck_full_analysis_batches_status",
        ),
        sa.CheckConstraint(
            "batch_index >= 0 AND core_start >= 0 AND core_end > core_start "
            "AND context_char_count >= 0",
            name="ck_full_analysis_batches_bounds",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(plan) = 'object' "
            "AND COALESCE(jsonb_typeof(plan->'slices'), '') = 'array' "
            "AND jsonb_array_length(plan->'slices') BETWEEN 1 AND 400",
            name="ck_full_analysis_batches_plan",
        ),
        sa.CheckConstraint(
            "((status = 'succeeded') = (result IS NOT NULL)) "
            "AND (result IS NULL OR (jsonb_typeof(result) = 'object' "
            "AND COALESCE(jsonb_typeof(result->'characters'), '') = 'array' "
            "AND COALESCE(jsonb_typeof(result->'summary'), '') = 'array'))",
            name="ck_full_analysis_batches_result",
        ),
        sa.CheckConstraint(
            "attempts >= 0 "
            "AND ((status = 'pending' AND attempt_token IS NULL) "
            "OR (status <> 'pending' AND attempts > 0 AND attempt_token IS NOT NULL))",
            name="ck_full_analysis_batches_attempt",
        ),
        sa.CheckConstraint(
            "length(btrim(provider)) > 0 AND length(btrim(model)) > 0 "
            "AND length(btrim(prompt_version)) > 0 "
            "AND (error_code IS NULL OR error_code ~ '^[a-z][a-z0-9_]{0,63}$')",
            name="ck_full_analysis_batches_text",
        ),
    )
    op.create_index(
        "ix_full_analysis_batches_status",
        "source_analysis_batches",
        [
            "tenant_id",
            "project_id",
            "source_document_id",
            "job_id",
            "status",
            "batch_index",
        ],
    )
    op.execute("""
        CREATE FUNCTION protect_full_analysis_job() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'source analysis jobs cannot be deleted';
          END IF;
          IF OLD.status IN ('succeeded', 'stale') THEN
            RAISE EXCEPTION 'terminal source analysis job is immutable';
          END IF;
          IF (to_jsonb(NEW) - ARRAY[
                'status', 'completed_batches', 'failed_batches', 'workflow_id',
                'final_analysis_id', 'error_code', 'updated_at'
              ]) IS DISTINCT FROM
             (to_jsonb(OLD) - ARRAY[
                'status', 'completed_batches', 'failed_batches', 'workflow_id',
                'final_analysis_id', 'error_code', 'updated_at'
              ]) THEN
            RAISE EXCEPTION 'source analysis job lineage is immutable';
          END IF;
          IF NEW.status <> OLD.status AND NOT (
               (OLD.status = 'queued' AND NEW.status IN ('running', 'failed', 'stale'))
            OR (OLD.status = 'running' AND NEW.status IN ('succeeded', 'failed', 'stale'))
            OR (OLD.status = 'failed' AND NEW.status IN ('queued', 'running', 'stale'))
          ) THEN
            RAISE EXCEPTION 'invalid source analysis job status transition';
          END IF;
          IF NEW.completed_batches < OLD.completed_batches THEN
            RAISE EXCEPTION 'completed source analysis batch count cannot decrease';
          END IF;
          IF NEW.failed_batches < OLD.failed_batches
             AND NOT (OLD.status = 'failed' AND NEW.status = 'queued') THEN
            RAISE EXCEPTION 'failed source analysis batch count can only reset for retry';
          END IF;
          IF OLD.final_analysis_id IS NOT NULL
             AND NEW.final_analysis_id IS DISTINCT FROM OLD.final_analysis_id THEN
            RAISE EXCEPTION 'final source analysis id is immutable';
          END IF;
          IF NEW.updated_at < OLD.updated_at THEN
            RAISE EXCEPTION 'source analysis job update time cannot decrease';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_full_analysis_job_guard
        BEFORE UPDATE OR DELETE ON source_analysis_jobs
        FOR EACH ROW EXECUTE FUNCTION protect_full_analysis_job()
    """)
    op.execute("""
        CREATE FUNCTION protect_full_analysis_batch() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'source analysis batches cannot be deleted';
          END IF;
          IF OLD.status = 'succeeded' THEN
            RAISE EXCEPTION 'succeeded source analysis batch is immutable';
          END IF;
          IF (to_jsonb(NEW) - ARRAY[
                'status', 'attempt_token', 'attempts', 'result', 'error_code', 'updated_at'
              ]) IS DISTINCT FROM
             (to_jsonb(OLD) - ARRAY[
                'status', 'attempt_token', 'attempts', 'result', 'error_code', 'updated_at'
              ]) THEN
            RAISE EXCEPTION 'source analysis batch plan and identity are immutable';
          END IF;
          IF NEW.status <> OLD.status AND NOT (
               (OLD.status = 'pending' AND NEW.status = 'running')
            OR (OLD.status = 'running' AND NEW.status IN ('succeeded', 'failed'))
            OR (OLD.status = 'failed' AND NEW.status IN ('pending', 'running'))
          ) THEN
            RAISE EXCEPTION 'invalid source analysis batch status transition';
          END IF;
          IF OLD.status IN ('pending', 'failed') AND NEW.status = 'running' THEN
            IF NEW.attempts <> OLD.attempts + 1
               OR NEW.attempt_token IS NULL
               OR NEW.attempt_token IS NOT DISTINCT FROM OLD.attempt_token THEN
              RAISE EXCEPTION 'source analysis batch claim requires a fresh token';
            END IF;
          ELSIF OLD.status = 'failed' AND NEW.status = 'pending' THEN
            IF NEW.attempts <> OLD.attempts OR NEW.attempt_token IS NOT NULL THEN
              RAISE EXCEPTION 'source analysis batch retry reset is invalid';
            END IF;
          ELSIF NEW.attempts <> OLD.attempts
                OR NEW.attempt_token IS DISTINCT FROM OLD.attempt_token THEN
            RAISE EXCEPTION 'source analysis batch attempt identity is immutable while claimed';
          END IF;
          IF NEW.result IS DISTINCT FROM OLD.result AND NEW.status <> 'succeeded' THEN
            RAISE EXCEPTION 'source analysis batch result requires success';
          END IF;
          IF NEW.updated_at < OLD.updated_at THEN
            RAISE EXCEPTION 'source analysis batch update time cannot decrease';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_full_analysis_batch_guard
        BEFORE UPDATE OR DELETE ON source_analysis_batches
        FOR EACH ROW EXECUTE FUNCTION protect_full_analysis_batch()
    """)


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM source_analysis_jobs) "
        "OR EXISTS (SELECT 1 FROM source_analysis_batches) THEN RAISE EXCEPTION "
        "'cannot downgrade full analysis jobs without losing data'; END IF; END $$"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM source_analysis_records "
        "WHERE jsonb_array_length(character_ids) > 30) OR EXISTS "
        "(SELECT 1 FROM source_analysis_reviews WHERE "
        "jsonb_array_length(snapshot->'characters') > 30) THEN RAISE EXCEPTION "
        "'cannot restore analysis history character limit while records exceed 30'; "
        "END IF; END $$"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_full_analysis_batch_guard ON source_analysis_batches"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_full_analysis_batch")
    op.execute("DROP TRIGGER IF EXISTS trg_full_analysis_job_guard ON source_analysis_jobs")
    op.execute("DROP FUNCTION IF EXISTS protect_full_analysis_job")
    op.drop_index("ix_full_analysis_batches_status", table_name="source_analysis_batches")
    op.drop_table("source_analysis_batches")
    op.drop_index("ix_full_analysis_jobs_history", table_name="source_analysis_jobs")
    op.drop_table("source_analysis_jobs")
    op.drop_constraint(
        "ck_analysis_records_json", "source_analysis_records", type_="check"
    )
    op.create_check_constraint(
        "ck_analysis_records_json",
        "source_analysis_records",
        "jsonb_typeof(original) = 'object' "
        "AND COALESCE(jsonb_typeof(original->'characters'), '') = 'array' "
        "AND COALESCE(jsonb_typeof(original->'summary'), '') = 'array' "
        "AND jsonb_typeof(character_ids) = 'array' "
        "AND jsonb_array_length(character_ids) = jsonb_array_length(original->'characters') "
        "AND jsonb_array_length(character_ids) <= 30",
    )
    op.drop_constraint(
        "ck_analysis_reviews_json", "source_analysis_reviews", type_="check"
    )
    op.create_check_constraint(
        "ck_analysis_reviews_json",
        "source_analysis_reviews",
        "jsonb_typeof(snapshot) = 'object' "
        "AND COALESCE(jsonb_typeof(snapshot->'characters'), '') = 'array' "
        "AND jsonb_array_length(snapshot->'characters') <= 30",
    )
