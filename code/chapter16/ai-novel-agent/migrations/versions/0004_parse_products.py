"""Version persisted parse products and preserve exact parser source references."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0004_parse_products"
down_revision = "0003_source_upload_sessions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _install_append_only_guard(*, product_columns: bool) -> None:
    parse_mutable = "'status', 'failure_code', 'started_at', 'completed_at'"
    if product_columns:
        parse_mutable += (
            ", 'product_version', 'chunker_name', 'chunker_config_sha256', "
            "'containment_policy', 'product_sha256'"
        )
    product_once = ""
    if product_columns:
        product_once = """
                IF OLD.product_version IS NOT NULL AND (
                    NEW.product_version IS DISTINCT FROM OLD.product_version
                    OR NEW.chunker_name IS DISTINCT FROM OLD.chunker_name
                    OR NEW.chunker_config_sha256 IS DISTINCT FROM OLD.chunker_config_sha256
                    OR NEW.containment_policy IS DISTINCT FROM OLD.containment_policy
                    OR NEW.product_sha256 IS DISTINCT FROM OLD.product_sha256
                ) THEN
                    RAISE EXCEPTION 'parse product identity can only be set once'
                        USING ERRCODE = 'check_violation';
                END IF;
        """
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION prevent_source_evidence_update()
        RETURNS trigger LANGUAGE plpgsql AS $$
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
                IF (to_jsonb(NEW) - ARRAY['status', 'failure_code', 'accepted_at',
                    'active_parse_run_id', 'aggregate_version']) IS DISTINCT FROM
                   (to_jsonb(OLD) - ARRAY['status', 'failure_code', 'accepted_at',
                    'active_parse_run_id', 'aggregate_version']) THEN
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
                IF (to_jsonb(NEW) - ARRAY[{parse_mutable}]) IS DISTINCT FROM
                   (to_jsonb(OLD) - ARRAY[{parse_mutable}]) THEN
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
                {product_once}
            ELSE
                RAISE EXCEPTION '% rows are append-only', TG_TABLE_NAME
                    USING ERRCODE = 'check_violation';
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )


def upgrade() -> None:
    op.drop_constraint("ck_source_docs_aggregate_version", "source_documents", type_="check")
    op.create_check_constraint(
        "ck_source_docs_aggregate_version",
        "source_documents",
        "aggregate_version >= 0 AND ("
        "(status IN ('upload_pending', 'uploaded') AND aggregate_version = 0) OR "
        "(status IN ('accepted', 'parsing') AND aggregate_version = 1) OR "
        "(status = 'parsed' AND aggregate_version = 2) OR "
        "(status = 'chunks_ready' AND aggregate_version >= 3) OR "
        "status IN ('failed', 'quarantined'))",
    )
    for name, column_type in (
        ("chunker_name", sa.String(length=64)),
        ("chunker_config_sha256", sa.String(length=64)),
        ("containment_policy", sa.String(length=64)),
        ("product_version", sa.Integer()),
        ("product_sha256", sa.String(length=64)),
    ):
        op.add_column("source_parse_runs", sa.Column(name, column_type, nullable=True))
    op.create_unique_constraint(
        "uq_source_parse_product_version",
        "source_parse_runs",
        ["tenant_id", "project_id", "source_document_id", "product_version"],
    )
    op.create_check_constraint(
        "ck_source_parse_product_identity",
        "source_parse_runs",
        "(product_version IS NULL AND chunker_name IS NULL "
        "AND chunker_config_sha256 IS NULL AND containment_policy IS NULL "
        "AND product_sha256 IS NULL) OR "
        "(product_version >= 3 AND length(btrim(chunker_name)) > 0 "
        "AND chunker_config_sha256 ~ '^[0-9a-f]{64}$' "
        "AND product_sha256 ~ '^[0-9a-f]{64}$' "
        "AND containment_policy = 'single-section-v1')",
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM source_parse_runs "
        "WHERE status IN ('pending', 'running') GROUP BY tenant_id, project_id, "
        "source_document_id HAVING count(*) > 1) THEN RAISE EXCEPTION "
        "'cannot create one-inflight index: resolve duplicate pending/running parse runs'; "
        "END IF; END $$"
    )
    op.create_index(
        "uq_source_parse_one_inflight",
        "source_parse_runs",
        ["tenant_id", "project_id", "source_document_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'running')"),
    )
    for table_name in ("source_sections", "source_chunks"):
        op.add_column(table_name, sa.Column("source_ref_kind", sa.String(20), nullable=True))
        op.add_column(table_name, sa.Column("source_ref_index", sa.Integer(), nullable=True))
        op.add_column(table_name, sa.Column("source_ref_subindex", sa.Integer(), nullable=True))
        op.create_check_constraint(
            f"ck_{table_name}_source_ref",
            table_name,
            "(source_ref_kind IS NULL AND source_ref_index IS NULL "
            "AND source_ref_subindex IS NULL) OR "
            "(source_ref_kind IN ('paragraph', 'page', 'table_row') "
            "AND source_ref_index >= 0 AND "
            "((source_ref_kind = 'table_row' AND source_ref_subindex >= 0) OR "
            "(source_ref_kind IN ('paragraph', 'page') AND source_ref_subindex IS NULL)))",
        )
    _install_append_only_guard(product_columns=True)


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM source_documents WHERE aggregate_version > 3) "
        "OR EXISTS (SELECT 1 FROM source_parse_runs WHERE product_version IS NOT NULL "
        "OR product_sha256 IS NOT NULL OR chunker_name IS NOT NULL "
        "OR chunker_config_sha256 IS NOT NULL OR containment_policy IS NOT NULL) "
        "OR EXISTS (SELECT 1 FROM source_sections WHERE source_ref_kind IS NOT NULL "
        "OR source_ref_index IS NOT NULL OR source_ref_subindex IS NOT NULL) "
        "OR EXISTS (SELECT 1 FROM source_chunks WHERE source_ref_kind IS NOT NULL "
        "OR source_ref_index IS NOT NULL OR source_ref_subindex IS NOT NULL) "
        "THEN RAISE EXCEPTION 'cannot downgrade parse products without losing data'; "
        "END IF; END $$"
    )
    _install_append_only_guard(product_columns=False)
    for table_name in ("source_chunks", "source_sections"):
        op.drop_constraint(f"ck_{table_name}_source_ref", table_name, type_="check")
        op.drop_column(table_name, "source_ref_subindex")
        op.drop_column(table_name, "source_ref_index")
        op.drop_column(table_name, "source_ref_kind")
    op.drop_index("uq_source_parse_one_inflight", table_name="source_parse_runs")
    op.drop_constraint(
        "ck_source_parse_product_identity", "source_parse_runs", type_="check"
    )
    op.drop_constraint(
        "uq_source_parse_product_version", "source_parse_runs", type_="unique"
    )
    for name in (
        "product_version",
        "product_sha256",
        "containment_policy",
        "chunker_config_sha256",
        "chunker_name",
    ):
        op.drop_column("source_parse_runs", name)
    op.drop_constraint("ck_source_docs_aggregate_version", "source_documents", type_="check")
    op.create_check_constraint(
        "ck_source_docs_aggregate_version",
        "source_documents",
        "aggregate_version BETWEEN 0 AND 3 AND ("
        "(status IN ('upload_pending', 'uploaded') AND aggregate_version = 0) OR "
        "(status IN ('accepted', 'parsing') AND aggregate_version = 1) OR "
        "(status = 'parsed' AND aggregate_version = 2) OR "
        "(status = 'chunks_ready' AND aggregate_version = 3) OR "
        "status IN ('failed', 'quarantined'))",
    )
