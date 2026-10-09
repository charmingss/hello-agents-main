"""Add immutable source-analysis history and append-only human reviews."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_analysis_history"
down_revision = "0007_projection_delivery"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_source_parse_runs_scope_source_id",
        "source_parse_runs",
        ["tenant_id", "project_id", "source_document_id", "id"],
    )
    op.create_table(
        "source_analysis_records",
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
        sa.Column("original", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("character_ids", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("created_by_subject", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "source_document_id", "id",
            name="uq_analysis_records_scope_id",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_document_id"],
            ["source_documents.tenant_id", "source_documents.project_id", "source_documents.id"],
            name="fk_analysis_records_source",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_document_id", "parse_run_id"],
            [
                "source_parse_runs.tenant_id", "source_parse_runs.project_id",
                "source_parse_runs.source_document_id", "source_parse_runs.id",
            ],
            name="fk_analysis_records_parse",
        ),
        sa.CheckConstraint(
            "product_version >= 3 AND current_revision >= 0",
            name="ck_analysis_records_versions",
        ),
        sa.CheckConstraint(
            "product_sha256 ~ '^[0-9a-f]{64}$'", name="ck_analysis_records_digest"
        ),
        sa.CheckConstraint(
            "length(btrim(provider)) > 0 AND length(btrim(model)) > 0 "
            "AND length(btrim(prompt_version)) > 0 "
            "AND length(btrim(created_by_subject)) > 0",
            name="ck_analysis_records_text",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(original) = 'object' "
            "AND COALESCE(jsonb_typeof(original->'characters'), '') = 'array' "
            "AND COALESCE(jsonb_typeof(original->'summary'), '') = 'array' "
            "AND jsonb_typeof(character_ids) = 'array' "
            "AND jsonb_array_length(character_ids) = jsonb_array_length(original->'characters') "
            "AND jsonb_array_length(character_ids) <= 30",
            name="ck_analysis_records_json",
        ),
    )
    op.create_index(
        "ix_analysis_records_history", "source_analysis_records",
        ["tenant_id", "project_id", "source_document_id", "created_at", "id"],
    )
    op.create_table(
        "source_analysis_reviews",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("source_document_id", sa.Uuid(), nullable=False),
        sa.Column("analysis_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_document_id", "analysis_id"],
            [
                "source_analysis_records.tenant_id", "source_analysis_records.project_id",
                "source_analysis_records.source_document_id", "source_analysis_records.id",
            ],
            name="fk_analysis_reviews_record",
        ),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "source_document_id", "analysis_id", "revision",
            name="uq_analysis_reviews_scope_revision",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_analysis_reviews_revision"),
        sa.CheckConstraint(
            "length(btrim(subject)) > 0", name="ck_analysis_reviews_subject"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot) = 'object' "
            "AND COALESCE(jsonb_typeof(snapshot->'characters'), '') = 'array' "
            "AND jsonb_array_length(snapshot->'characters') <= 30",
            name="ck_analysis_reviews_json",
        ),
    )
    op.execute("""
        CREATE FUNCTION protect_source_analysis_record() RETURNS trigger AS $$
        BEGIN
          IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'source analysis records cannot be deleted';
          END IF;
          IF NEW.current_revision <> OLD.current_revision + 1
             OR (to_jsonb(NEW) - 'current_revision')
                IS DISTINCT FROM (to_jsonb(OLD) - 'current_revision') THEN
            RAISE EXCEPTION 'source analysis original and lineage are immutable';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_source_analysis_record_immutable
        BEFORE UPDATE OR DELETE ON source_analysis_records
        FOR EACH ROW EXECUTE FUNCTION protect_source_analysis_record()
    """)
    op.execute("""
        CREATE FUNCTION protect_source_analysis_review() RETURNS trigger AS $$
        BEGIN
          RAISE EXCEPTION 'source analysis reviews are append only';
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_source_analysis_review_append_only
        BEFORE UPDATE OR DELETE ON source_analysis_reviews
        FOR EACH ROW EXECUTE FUNCTION protect_source_analysis_review()
    """)


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM source_analysis_records) "
        "OR EXISTS (SELECT 1 FROM source_analysis_reviews) THEN RAISE EXCEPTION "
        "'cannot downgrade analysis history without losing data'; END IF; END $$"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_source_analysis_review_append_only "
               "ON source_analysis_reviews")
    op.execute("DROP FUNCTION IF EXISTS protect_source_analysis_review")
    op.execute("DROP TRIGGER IF EXISTS trg_source_analysis_record_immutable "
               "ON source_analysis_records")
    op.execute("DROP FUNCTION IF EXISTS protect_source_analysis_record")
    op.drop_table("source_analysis_reviews")
    op.drop_index("ix_analysis_records_history", table_name="source_analysis_records")
    op.drop_table("source_analysis_records")
    op.drop_constraint(
        "uq_source_parse_runs_scope_source_id", "source_parse_runs", type_="unique"
    )
