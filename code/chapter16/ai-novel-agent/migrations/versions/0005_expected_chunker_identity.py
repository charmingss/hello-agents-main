"""Persist expected chunker identity before parse products are produced."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0005_expected_chunker_identity"
down_revision = "0004_parse_products"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM source_parse_runs WHERE "
        "status IN ('pending', 'running')) THEN "
        "RAISE EXCEPTION 'cannot migrate inflight parse runs: expected chunker identity "
        "is not historically provable'; END IF; END $$"
    )
    op.drop_constraint("uq_source_parse_identity", "source_parse_runs", type_="unique")
    op.drop_constraint("ck_source_parse_runs_profile", "source_parse_runs", type_="check")
    op.add_column(
        "source_parse_runs",
        sa.Column(
            "expected_chunker_name",
            sa.String(length=64),
            nullable=True,
        ),
    )
    op.add_column(
        "source_parse_runs",
        sa.Column(
            "expected_chunker_config_sha256",
            sa.String(length=64),
            nullable=True,
        ),
    )
    op.add_column(
        "source_parse_runs",
        sa.Column(
            "expected_containment_policy",
            sa.String(length=64),
            nullable=True,
        ),
    )
    op.execute(
        "UPDATE source_parse_runs SET expected_chunker_name = chunker_name, "
        "expected_chunker_config_sha256 = chunker_config_sha256, "
        "expected_containment_policy = containment_policy "
        "WHERE product_version IS NOT NULL AND chunker_name IS NOT NULL "
        "AND chunker_config_sha256 IS NOT NULL AND containment_policy IS NOT NULL"
    )
    op.create_check_constraint(
        "ck_source_parse_runs_profile",
        "source_parse_runs",
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
    )
    op.create_unique_constraint(
        "uq_source_parse_identity",
        "source_parse_runs",
        [
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
        ],
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM source_parse_runs WHERE "
        "expected_chunker_name IS NOT NULL AND (chunker_name IS NULL OR "
        "expected_chunker_name IS DISTINCT FROM chunker_name OR "
        "expected_chunker_config_sha256 IS DISTINCT FROM chunker_config_sha256 OR "
        "expected_containment_policy IS DISTINCT FROM containment_policy)) THEN "
        "RAISE EXCEPTION 'cannot downgrade expected chunker identity without losing data'; "
        "END IF; END $$"
    )
    op.drop_constraint("uq_source_parse_identity", "source_parse_runs", type_="unique")
    op.drop_constraint("ck_source_parse_runs_profile", "source_parse_runs", type_="check")
    op.drop_column("source_parse_runs", "expected_containment_policy")
    op.drop_column("source_parse_runs", "expected_chunker_config_sha256")
    op.drop_column("source_parse_runs", "expected_chunker_name")
    op.create_check_constraint(
        "ck_source_parse_runs_profile",
        "source_parse_runs",
        "length(btrim(parser_name)) > 0 AND length(btrim(parser_version)) > 0 "
        "AND length(btrim(chunker_version)) > 0",
    )
    op.create_unique_constraint(
        "uq_source_parse_identity",
        "source_parse_runs",
        [
            "tenant_id",
            "project_id",
            "source_document_id",
            "source_content_sha256",
            "parser_name",
            "parser_version",
            "parser_config_sha256",
            "chunker_version",
        ],
    )
