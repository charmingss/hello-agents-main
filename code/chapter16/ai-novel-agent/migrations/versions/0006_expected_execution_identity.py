"""Persist workflow execution configuration identity for new parse runs."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0006_expected_execution_identity"
down_revision = "0005_expected_chunker_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM source_parse_runs WHERE "
        "status IN ('pending', 'running')) THEN "
        "RAISE EXCEPTION 'cannot migrate inflight parse runs: expected execution "
        "configuration is not historically provable'; END IF; END $$"
    )
    op.drop_constraint("uq_source_parse_identity", "source_parse_runs", type_="unique")
    op.add_column(
        "source_parse_runs",
        sa.Column("identity_schema_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "source_parse_runs",
        sa.Column(
            "expected_execution_config_sha256", sa.String(length=64), nullable=True
        ),
    )
    op.create_check_constraint(
        "ck_source_parse_runs_execution_identity",
        "source_parse_runs",
        "((identity_schema_version IS NULL "
        "AND expected_execution_config_sha256 IS NULL) OR "
        "(identity_schema_version = 1 "
        "AND expected_execution_config_sha256 ~ '^[0-9a-f]{64}$')) "
        "AND (status NOT IN ('pending', 'running') "
        "OR identity_schema_version = 1)",
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
            "identity_schema_version",
            "expected_execution_config_sha256",
        ],
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM source_parse_runs WHERE "
        "identity_schema_version IS NOT NULL OR "
        "expected_execution_config_sha256 IS NOT NULL) THEN "
        "RAISE EXCEPTION 'cannot downgrade execution identity without losing data'; "
        "END IF; END $$"
    )
    op.drop_constraint("uq_source_parse_identity", "source_parse_runs", type_="unique")
    op.drop_constraint(
        "ck_source_parse_runs_execution_identity", "source_parse_runs", type_="check"
    )
    op.drop_column("source_parse_runs", "expected_execution_config_sha256")
    op.drop_column("source_parse_runs", "identity_schema_version")
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
