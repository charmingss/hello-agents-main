"""Add durable projection delivery leases and complete projection lineage."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0007_projection_delivery"
down_revision = "0006_expected_execution_identity"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    for name, column_type in (
        ("lease_token", sa.Uuid()),
        ("lease_owner", sa.String(100)),
        ("lease_expires_at", sa.DateTime(timezone=True)),
        ("next_attempt_at", sa.DateTime(timezone=True)),
        ("dead_at", sa.DateTime(timezone=True)),
        ("dead_reason", sa.String(64)),
    ):
        op.add_column("outbox_events", sa.Column(name, column_type, nullable=True))
    op.create_check_constraint(
        "ck_outbox_projection_lease",
        "outbox_events",
        "(lease_token IS NULL AND lease_owner IS NULL AND lease_expires_at IS NULL) OR "
        "(lease_token IS NOT NULL AND lease_owner IS NOT NULL AND length(btrim(lease_owner)) > 0 "
        "AND lease_expires_at IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_outbox_dead_letter",
        "outbox_events",
        "(dead_at IS NULL AND dead_reason IS NULL) OR "
        "(dead_at IS NOT NULL AND dead_reason IS NOT NULL "
        "AND dead_reason ~ '^[a-z][a-z0-9_]{0,63}$')",
    )
    op.create_index(
        "ix_outbox_projection_claim",
        "outbox_events",
        ["published_at", "dead_at", "next_attempt_at", "occurred_at"],
    )

    for name, column_type in (
        ("source_document_id", sa.Uuid()),
        ("parse_run_id", sa.Uuid()),
        ("product_sha256", sa.String(64)),
        ("embedding_identity_sha256", sa.String(64)),
        ("vector_schema_version", sa.Integer()),
        ("lag_started_at", sa.DateTime(timezone=True)),
        ("last_succeeded_at", sa.DateTime(timezone=True)),
        ("attempt_token", sa.Uuid()),
    ):
        op.add_column("projection_checkpoints", sa.Column(name, column_type, nullable=True))
    op.add_column(
        "projection_checkpoints",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_check_constraint(
        "ck_projection_ckpts_lineage",
        "projection_checkpoints",
        "(source_document_id IS NULL AND parse_run_id IS NULL AND product_sha256 IS NULL "
        "AND embedding_identity_sha256 IS NULL AND vector_schema_version IS NULL) OR "
        "(source_document_id IS NOT NULL AND parse_run_id IS NOT NULL "
        "AND product_sha256 IS NOT NULL AND product_sha256 ~ '^[0-9a-f]{64}$' "
        "AND embedding_identity_sha256 IS NOT NULL "
        "AND embedding_identity_sha256 ~ '^[0-9a-f]{64}$' "
        "AND vector_schema_version IS NOT NULL AND vector_schema_version > 0)",
    )
    op.create_check_constraint(
        "ck_projection_ckpts_attempt_count",
        "projection_checkpoints",
        "attempt_count >= 0",
    )


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM outbox_events WHERE lease_token IS NOT NULL "
        "OR lease_owner IS NOT NULL OR lease_expires_at IS NOT NULL "
        "OR dead_at IS NOT NULL OR dead_reason IS NOT NULL "
        "OR next_attempt_at IS NOT NULL) OR EXISTS "
        "(SELECT 1 FROM projection_checkpoints WHERE source_document_id IS NOT NULL "
        "OR parse_run_id IS NOT NULL OR product_sha256 IS NOT NULL "
        "OR embedding_identity_sha256 IS NOT NULL OR vector_schema_version IS NOT NULL "
        "OR lag_started_at IS NOT NULL OR last_succeeded_at IS NOT NULL "
        "OR attempt_token IS NOT NULL OR attempt_count <> 0) THEN RAISE EXCEPTION "
        "'cannot downgrade active projection delivery state without losing data'; "
        "END IF; END $$"
    )
    op.drop_constraint(
        "ck_projection_ckpts_attempt_count", "projection_checkpoints", type_="check"
    )
    op.drop_constraint("ck_projection_ckpts_lineage", "projection_checkpoints", type_="check")
    for name in (
        "attempt_count",
        "attempt_token",
        "last_succeeded_at",
        "lag_started_at",
        "vector_schema_version",
        "embedding_identity_sha256",
        "product_sha256",
        "parse_run_id",
        "source_document_id",
    ):
        op.drop_column("projection_checkpoints", name)
    op.drop_index("ix_outbox_projection_claim", table_name="outbox_events")
    op.drop_constraint("ck_outbox_dead_letter", "outbox_events", type_="check")
    op.drop_constraint("ck_outbox_projection_lease", "outbox_events", type_="check")
    for name in (
        "dead_reason",
        "dead_at",
        "next_attempt_at",
        "lease_expires_at",
        "lease_owner",
        "lease_token",
    ):
        op.drop_column("outbox_events", name)
