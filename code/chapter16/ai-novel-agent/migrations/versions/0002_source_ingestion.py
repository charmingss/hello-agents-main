"""Add authoritative source-ingestion evidence and projection checkpoints.

Revision ID: 0002_source_ingestion
Revises: 0001_platform_foundation
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0002_source_ingestion"
down_revision: str | None = "0001_platform_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _scope_columns() -> tuple[sa.Column[object], sa.Column[object]]:
    return (
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
    )


def _scope_foreign_keys(name: str) -> tuple[sa.ForeignKeyConstraint, sa.ForeignKeyConstraint]:
    return (
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name=name,
        ),
    )


def upgrade() -> None:
    op.drop_constraint(
        "uq_outbox_events_tenant_aggregate_version_event",
        "outbox_events",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_outbox_events_tenant_aggregate_version",
        "outbox_events",
        ["tenant_id", "aggregate_type", "aggregate_id", "aggregate_version"],
    )
    op.create_table(
        "source_documents",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_scope_columns(),
        sa.Column("upload_request_id", sa.String(length=100), nullable=False),
        sa.Column(
            "authorization_provenance",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
        ),
        sa.Column("authorization_sha256", sa.String(length=64), nullable=False),
        sa.Column("original_display_name", sa.String(length=255), nullable=False),
        sa.Column("declared_media_type", sa.String(length=100), nullable=False),
        sa.Column("detected_media_type", sa.String(length=100), nullable=False),
        sa.Column("byte_count", sa.BigInteger(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("object_key", sa.String(length=500), nullable=False),
        sa.Column("active_parse_run_id", sa.Uuid(), nullable=True),
        sa.Column("aggregate_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column(
            "status", sa.String(length=30), server_default="upload_pending", nullable=False
        ),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=True),
        *_scope_foreign_keys("fk_source_docs_tenant_project"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "project_id", "id", name="uq_source_docs_scope_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "id",
            "content_sha256",
            name="uq_source_docs_scope_id_content",
        ),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "upload_request_id", name="uq_source_docs_scope_request"
        ),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "content_sha256", name="uq_source_docs_scope_content"
        ),
        sa.UniqueConstraint("object_key", name="uq_source_docs_object_key"),
        sa.CheckConstraint("byte_count > 0", name="ck_source_docs_byte_count"),
        sa.CheckConstraint(
            "length(btrim(upload_request_id)) > 0 "
            "AND length(btrim(original_display_name)) > 0 "
            "AND length(btrim(declared_media_type)) > 0 "
            "AND length(btrim(detected_media_type)) > 0 "
            "AND jsonb_typeof(authorization_provenance) = 'object'",
            name="ck_source_docs_required_text",
        ),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$' AND authorization_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_source_docs_hashes",
        ),
        sa.CheckConstraint(
            "object_key ~ '^[A-Za-z0-9][A-Za-z0-9/_-]{0,499}$'",
            name="ck_source_docs_object_key",
        ),
        sa.CheckConstraint(
            "status IN ('upload_pending', 'uploaded', 'accepted', 'parsing', 'parsed', "
            "'chunks_ready', 'failed', 'quarantined')",
            name="ck_source_docs_status",
        ),
        sa.CheckConstraint(
            "((status IN ('failed', 'quarantined')) = (failure_code IS NOT NULL)) "
            "AND (failure_code IS NULL OR failure_code ~ '^[a-z][a-z0-9_]{0,63}$')",
            name="ck_source_docs_failure_state",
        ),
        sa.CheckConstraint(
            "status NOT IN ('accepted', 'parsing', 'parsed', 'chunks_ready') "
            "OR accepted_at IS NOT NULL",
            name="ck_source_docs_lifecycle_time",
        ),
        sa.CheckConstraint(
            "(status IN ('parsing', 'parsed', 'chunks_ready') AND active_parse_run_id IS NOT NULL) "
            "OR status NOT IN ('parsing', 'parsed', 'chunks_ready')",
            name="ck_source_docs_active_parse",
        ),
        sa.CheckConstraint(
            "aggregate_version BETWEEN 0 AND 3 AND ("
            "(status IN ('upload_pending', 'uploaded') AND aggregate_version = 0) OR "
            "(status IN ('accepted', 'parsing') AND aggregate_version = 1) OR "
            "(status = 'parsed' AND aggregate_version = 2) OR "
            "(status = 'chunks_ready' AND aggregate_version = 3) OR "
            "status IN ('failed', 'quarantined'))",
            name="ck_source_docs_aggregate_version",
        ),
    )
    op.create_index("ix_source_documents_tenant_id", "source_documents", ["tenant_id"])
    op.create_index("ix_source_documents_project_id", "source_documents", ["project_id"])
    op.create_index(
        "ix_source_docs_scope_status",
        "source_documents",
        ["tenant_id", "project_id", "status"],
    )

    op.create_table(
        "source_parse_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_scope_columns(),
        sa.Column("source_document_id", sa.Uuid(), nullable=False),
        sa.Column("source_content_sha256", sa.String(length=64), nullable=False),
        sa.Column("parser_name", sa.String(length=64), nullable=False),
        sa.Column("parser_version", sa.String(length=64), nullable=False),
        sa.Column("parser_config_sha256", sa.String(length=64), nullable=False),
        sa.Column("chunker_version", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="pending", nullable=False),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_scope_foreign_keys("fk_source_parse_runs_tenant_project"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_document_id", "source_content_sha256"],
            [
                "source_documents.tenant_id",
                "source_documents.project_id",
                "source_documents.id",
                "source_documents.content_sha256",
            ],
            name="fk_source_parse_runs_source_doc",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "id", name="uq_source_parse_runs_scope_id"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "source_document_id",
            "source_content_sha256",
            "parser_name",
            "parser_version",
            "parser_config_sha256",
            "chunker_version",
            name="uq_source_parse_identity",
        ),
        sa.CheckConstraint(
            "source_content_sha256 ~ '^[0-9a-f]{64}$' "
            "AND parser_config_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_source_parse_runs_hashes",
        ),
        sa.CheckConstraint(
            "length(btrim(parser_name)) > 0 AND length(btrim(parser_version)) > 0 "
            "AND length(btrim(chunker_version)) > 0",
            name="ck_source_parse_runs_profile",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'running', 'succeeded', 'failed', 'superseded')",
            name="ck_source_parse_runs_status",
        ),
        sa.CheckConstraint(
            "((status = 'failed') = (failure_code IS NOT NULL)) "
            "AND (failure_code IS NULL OR failure_code ~ '^[a-z][a-z0-9_]{0,63}$')",
            name="ck_source_parse_runs_failure_state",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND started_at IS NULL AND completed_at IS NULL) "
            "OR (status = 'running' AND started_at IS NOT NULL AND completed_at IS NULL) "
            "OR (status IN ('succeeded', 'superseded') AND started_at IS NOT NULL "
            "AND completed_at IS NOT NULL) "
            "OR (status = 'failed' AND completed_at IS NOT NULL)",
            name="ck_source_parse_runs_lifecycle_time",
        ),
    )
    op.create_index("ix_source_parse_runs_tenant_id", "source_parse_runs", ["tenant_id"])
    op.create_index("ix_source_parse_runs_project_id", "source_parse_runs", ["project_id"])
    op.create_index(
        "ix_source_parse_runs_source_document_id",
        "source_parse_runs",
        ["source_document_id"],
    )
    op.create_index(
        "ix_source_parse_runs_scope_status",
        "source_parse_runs",
        ["tenant_id", "project_id", "status"],
    )

    location_check = """
        char_start >= 0 AND char_end > char_start
        AND ((page_start IS NULL AND page_end IS NULL)
             OR (page_start >= 1 AND page_end >= page_start))
        AND ((paragraph_start IS NULL AND paragraph_end IS NULL)
             OR (paragraph_start >= 0 AND paragraph_end >= paragraph_start))
    """
    op.create_table(
        "source_sections",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_scope_columns(),
        sa.Column("parse_run_id", sa.Uuid(), nullable=False),
        sa.Column("section_index", sa.Integer(), nullable=False),
        sa.Column("heading", sa.String(length=500), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("char_start", sa.BigInteger(), nullable=False),
        sa.Column("char_end", sa.BigInteger(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("paragraph_start", sa.Integer(), nullable=True),
        sa.Column("paragraph_end", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        *_scope_foreign_keys("fk_source_sections_tenant_project"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "parse_run_id"],
            ["source_parse_runs.tenant_id", "source_parse_runs.project_id", "source_parse_runs.id"],
            name="fk_source_sections_parse_run",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "id", name="uq_source_sections_scope_id"
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "parse_run_id",
            "section_index",
            name="uq_source_sections_parse_index",
        ),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "parse_run_id", "id", name="uq_source_sections_parse_id"
        ),
        sa.CheckConstraint("section_index >= 0", name="ck_source_sections_index"),
        sa.CheckConstraint(
            "length(btrim(text)) > 0 AND (heading IS NULL OR length(btrim(heading)) > 0)",
            name="ck_source_sections_text",
        ),
        sa.CheckConstraint(location_check, name="ck_source_sections_location"),
    )
    op.create_index("ix_source_sections_tenant_id", "source_sections", ["tenant_id"])
    op.create_index("ix_source_sections_project_id", "source_sections", ["project_id"])
    op.create_index("ix_source_sections_parse_run_id", "source_sections", ["parse_run_id"])
    op.create_index(
        "ix_source_sections_scope_parse",
        "source_sections",
        ["tenant_id", "project_id", "parse_run_id"],
    )

    op.create_table(
        "source_chunks",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_scope_columns(),
        sa.Column("parse_run_id", sa.Uuid(), nullable=False),
        sa.Column("section_id", sa.Uuid(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_count", sa.Integer(), nullable=True),
        sa.Column("char_start", sa.BigInteger(), nullable=False),
        sa.Column("char_end", sa.BigInteger(), nullable=False),
        sa.Column("page_start", sa.Integer(), nullable=True),
        sa.Column("page_end", sa.Integer(), nullable=True),
        sa.Column("paragraph_start", sa.Integer(), nullable=True),
        sa.Column("paragraph_end", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        *_scope_foreign_keys("fk_source_chunks_tenant_project"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "parse_run_id"],
            ["source_parse_runs.tenant_id", "source_parse_runs.project_id", "source_parse_runs.id"],
            name="fk_source_chunks_parse_run",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "parse_run_id", "section_id"],
            [
                "source_sections.tenant_id",
                "source_sections.project_id",
                "source_sections.parse_run_id",
                "source_sections.id",
            ],
            name="fk_source_chunks_section",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "project_id", "id", name="uq_source_chunks_scope_id"),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "parse_run_id",
            "section_id",
            "char_start",
            "char_end",
            name="uq_source_chunks_parse_span",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "parse_run_id",
            "chunk_index",
            name="uq_source_chunks_parse_index",
        ),
        sa.CheckConstraint("chunk_index >= 0", name="ck_source_chunks_index"),
        sa.CheckConstraint(
            "token_count IS NULL OR token_count > 0", name="ck_source_chunks_tokens"
        ),
        sa.CheckConstraint("length(btrim(text)) > 0", name="ck_source_chunks_text"),
        sa.CheckConstraint(location_check, name="ck_source_chunks_location"),
    )
    op.create_index("ix_source_chunks_tenant_id", "source_chunks", ["tenant_id"])
    op.create_index("ix_source_chunks_project_id", "source_chunks", ["project_id"])
    op.create_index("ix_source_chunks_parse_run_id", "source_chunks", ["parse_run_id"])
    op.create_index("ix_source_chunks_section_id", "source_chunks", ["section_id"])
    op.create_index(
        "ix_source_chunks_scope_parse",
        "source_chunks",
        ["tenant_id", "project_id", "parse_run_id"],
    )

    op.create_table(
        "projection_checkpoints",
        sa.Column("id", sa.Uuid(), nullable=False),
        *_scope_columns(),
        sa.Column("projection_name", sa.String(length=64), nullable=False),
        sa.Column("checkpoint_key", sa.String(length=200), nullable=False),
        sa.Column("projection_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("status", sa.String(length=30), server_default="lagging", nullable=False),
        sa.Column("last_applied_event_id", sa.Uuid(), nullable=True),
        sa.Column("last_applied_event_occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("attempted_version", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_attempted_event_id", sa.Uuid(), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        *_scope_foreign_keys("fk_projection_ckpts_tenant_project"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "projection_name",
            "checkpoint_key",
            name="uq_projection_ckpts_scope_name_key",
        ),
        sa.CheckConstraint("projection_version >= 0", name="ck_projection_ckpts_version"),
        sa.CheckConstraint(
            "attempted_version >= projection_version",
            name="ck_projection_ckpts_attempted_version",
        ),
        sa.CheckConstraint(
            "((projection_version = 0 AND last_applied_event_id IS NULL "
            "AND last_applied_event_occurred_at IS NULL) "
            "OR (projection_version > 0 AND last_applied_event_id IS NOT NULL "
            "AND last_applied_event_occurred_at IS NOT NULL)) "
            "AND ((attempted_version = 0 AND last_attempted_event_id IS NULL) "
            "OR (attempted_version > 0 AND last_attempted_event_id IS NOT NULL))",
            name="ck_projection_ckpts_event_identity",
        ),
        sa.CheckConstraint(
            "length(btrim(projection_name)) > 0 AND length(btrim(checkpoint_key)) > 0",
            name="ck_projection_ckpts_required_text",
        ),
        sa.CheckConstraint(
            "status IN ('current', 'lagging', 'failed', 'rebuilding')",
            name="ck_projection_ckpts_status",
        ),
        sa.CheckConstraint(
            "((status = 'failed') = (failure_code IS NOT NULL)) "
            "AND (failure_code IS NULL OR failure_code ~ '^[a-z][a-z0-9_]{0,63}$')",
            name="ck_projection_ckpts_failure_state",
        ),
    )
    op.create_index(
        "ix_projection_checkpoints_tenant_id", "projection_checkpoints", ["tenant_id"]
    )
    op.create_index(
        "ix_projection_checkpoints_project_id", "projection_checkpoints", ["project_id"]
    )
    op.create_index(
        "ix_projection_ckpts_scope_status",
        "projection_checkpoints",
        ["tenant_id", "project_id", "status"],
    )

    op.execute(
        """
        CREATE FUNCTION prevent_source_evidence_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF TG_TABLE_NAME = 'source_documents'
                   AND (NEW.active_parse_run_id IS NOT NULL
                        OR NEW.status NOT IN ('upload_pending', 'accepted')) THEN
                    RAISE EXCEPTION 'source document insert must not claim an active parse'
                        USING ERRCODE = 'check_violation';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                IF current_setting('novel_agent.allow_evidence_delete', true) = 'on' THEN
                    RETURN OLD;
                END IF;
                RAISE EXCEPTION '% rows require controlled deletion', TG_TABLE_NAME
                    USING ERRCODE = 'insufficient_privilege';
            END IF;

            IF TG_TABLE_NAME = 'source_documents' THEN
                -- source status fields are mutable; accepted evidence and identity are not.
                IF (to_jsonb(NEW) -
                    ARRAY['status', 'failure_code', 'accepted_at', 'active_parse_run_id',
                          'aggregate_version'])
                   IS DISTINCT FROM
                   (to_jsonb(OLD) -
                    ARRAY['status', 'failure_code', 'accepted_at', 'active_parse_run_id',
                          'aggregate_version']) THEN
                    RAISE EXCEPTION 'source document evidence is append-only'
                        USING ERRCODE = 'check_violation';
                END IF;
                IF OLD.accepted_at IS NOT NULL
                   AND NEW.accepted_at IS DISTINCT FROM OLD.accepted_at THEN
                    RAISE EXCEPTION 'accepted_at can only be set once'
                        USING ERRCODE = 'check_violation';
                END IF;
                IF NEW.active_parse_run_id IS NOT NULL AND NOT EXISTS (
                    SELECT 1 FROM source_parse_runs AS active_parse
                    WHERE active_parse.id = NEW.active_parse_run_id
                      AND active_parse.tenant_id = NEW.tenant_id
                      AND active_parse.project_id = NEW.project_id
                      AND active_parse.source_document_id = NEW.id
                      AND active_parse.source_content_sha256 = NEW.content_sha256
                ) THEN
                    RAISE EXCEPTION 'active_parse_run_id must belong to source and scope'
                        USING ERRCODE = 'foreign_key_violation';
                END IF;
            ELSIF TG_TABLE_NAME = 'source_parse_runs' THEN
                IF (to_jsonb(NEW) - ARRAY['status', 'failure_code', 'started_at', 'completed_at'])
                   IS DISTINCT FROM
                   (to_jsonb(OLD) -
                    ARRAY['status', 'failure_code', 'started_at', 'completed_at']) THEN
                    RAISE EXCEPTION 'source parse identity is append-only'
                        USING ERRCODE = 'check_violation';
                END IF;
                IF (OLD.started_at IS NOT NULL
                    AND NEW.started_at IS DISTINCT FROM OLD.started_at)
                   OR (OLD.completed_at IS NOT NULL
                       AND NEW.completed_at IS DISTINCT FROM OLD.completed_at) THEN
                    RAISE EXCEPTION 'parse lifecycle timestamps can only be set once'
                        USING ERRCODE = 'check_violation';
                END IF;
            ELSE
                RAISE EXCEPTION '% rows are append-only', TG_TABLE_NAME
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_source_documents_append_only "
        "BEFORE INSERT OR UPDATE OR DELETE ON source_documents FOR EACH ROW "
        "EXECUTE FUNCTION prevent_source_evidence_update()"
    )
    for table_name in ("source_parse_runs", "source_sections", "source_chunks"):
        op.execute(
            f"CREATE TRIGGER trg_{table_name}_append_only "
            f"BEFORE UPDATE OR DELETE ON {table_name} FOR EACH ROW "
            "EXECUTE FUNCTION prevent_source_evidence_update()"
        )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_source_chunks_append_only ON source_chunks")
    op.execute("DROP TRIGGER IF EXISTS trg_source_sections_append_only ON source_sections")
    op.execute("DROP TRIGGER IF EXISTS trg_source_parse_runs_append_only ON source_parse_runs")
    op.execute("DROP TRIGGER IF EXISTS trg_source_documents_append_only ON source_documents")
    op.execute("DROP FUNCTION prevent_source_evidence_update()")

    op.drop_index("ix_projection_ckpts_scope_status", table_name="projection_checkpoints")
    op.drop_index("ix_projection_checkpoints_project_id", table_name="projection_checkpoints")
    op.drop_index("ix_projection_checkpoints_tenant_id", table_name="projection_checkpoints")
    op.drop_table("projection_checkpoints")

    op.drop_index("ix_source_chunks_scope_parse", table_name="source_chunks")
    op.drop_index("ix_source_chunks_section_id", table_name="source_chunks")
    op.drop_index("ix_source_chunks_parse_run_id", table_name="source_chunks")
    op.drop_index("ix_source_chunks_project_id", table_name="source_chunks")
    op.drop_index("ix_source_chunks_tenant_id", table_name="source_chunks")
    op.drop_table("source_chunks")

    op.drop_index("ix_source_sections_scope_parse", table_name="source_sections")
    op.drop_index("ix_source_sections_parse_run_id", table_name="source_sections")
    op.drop_index("ix_source_sections_project_id", table_name="source_sections")
    op.drop_index("ix_source_sections_tenant_id", table_name="source_sections")
    op.drop_table("source_sections")

    op.drop_index("ix_source_parse_runs_scope_status", table_name="source_parse_runs")
    op.drop_index("ix_source_parse_runs_source_document_id", table_name="source_parse_runs")
    op.drop_index("ix_source_parse_runs_project_id", table_name="source_parse_runs")
    op.drop_index("ix_source_parse_runs_tenant_id", table_name="source_parse_runs")
    op.drop_table("source_parse_runs")

    op.drop_index("ix_source_docs_scope_status", table_name="source_documents")
    op.drop_index("ix_source_documents_project_id", table_name="source_documents")
    op.drop_index("ix_source_documents_tenant_id", table_name="source_documents")
    op.drop_table("source_documents")
    op.drop_constraint(
        "uq_outbox_events_tenant_aggregate_version",
        "outbox_events",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_outbox_events_tenant_aggregate_version_event",
        "outbox_events",
        [
            "tenant_id",
            "aggregate_type",
            "aggregate_id",
            "aggregate_version",
            "event_type",
        ],
    )
