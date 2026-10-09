"""Add durable upload-session claims.

Revision ID: 0003_source_upload_sessions
Revises: 0002_source_ingestion
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003_source_upload_sessions"
down_revision: str | None = "0002_source_ingestion"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "source_upload_sessions",
        sa.Column("upload_id", sa.Uuid(), nullable=False),
        sa.Column("source_document_id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("request_id", sa.String(length=100), nullable=False),
        sa.Column("original_display_name", sa.String(length=255), nullable=False),
        sa.Column("file_type", sa.String(length=10), nullable=False),
        sa.Column("declared_media_type", sa.String(length=100), nullable=False),
        sa.Column("byte_count", sa.BigInteger(), nullable=False),
        sa.Column("content_sha256", sa.String(length=64), nullable=False),
        sa.Column("authorization_declaration", postgresql.JSONB(), nullable=False),
        sa.Column("declared_by_subject", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("completion_request_id", sa.String(length=100), nullable=True),
        sa.Column("attempt_token", sa.Uuid(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_source_upload_sessions_tenant_project",
        ),
        sa.PrimaryKeyConstraint("upload_id"),
        sa.UniqueConstraint("source_document_id"),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "request_id", name="uq_source_upload_scope_request"
        ),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "upload_id", name="uq_source_upload_scope_upload"
        ),
        sa.CheckConstraint("byte_count > 0", name="ck_source_upload_byte_count"),
        sa.CheckConstraint(
            "content_sha256 ~ '^[0-9a-f]{64}$'", name="ck_source_upload_content_hash"
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'validating', 'consumed', 'expired')",
            name="ck_source_upload_status",
        ),
        sa.CheckConstraint(
            "(status = 'validating' AND attempt_token IS NOT NULL "
            "AND completion_request_id IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'validating')",
            name="ck_source_upload_validation_lease",
        ),
        sa.CheckConstraint(
            "(status = 'consumed' AND consumed_at IS NOT NULL "
            "AND completion_request_id IS NOT NULL AND attempt_token IS NULL "
            "AND lease_expires_at IS NULL) OR (status <> 'consumed' AND consumed_at IS NULL)",
            name="ck_source_upload_consumption",
        ),
        sa.CheckConstraint(
            "length(btrim(declared_by_subject)) BETWEEN 1 AND 255 "
            "AND declared_by_subject !~ '[[:cntrl:]]'",
            name="ck_source_upload_declared_subject",
        ),
        sa.CheckConstraint(
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
    )
    op.create_index(
        "ix_source_upload_scope_status",
        "source_upload_sessions",
        ["tenant_id", "project_id", "status", "expires_at"],
    )
    op.create_index(
        "ix_source_upload_sessions_tenant_id", "source_upload_sessions", ["tenant_id"]
    )
    op.create_index(
        "ix_source_upload_sessions_project_id", "source_upload_sessions", ["project_id"]
    )
    op.execute(
        """
        CREATE FUNCTION prevent_upload_declaration_update()
        RETURNS trigger
        LANGUAGE plpgsql
        AS $$
        BEGIN
            IF (to_jsonb(NEW) - ARRAY['status', 'completion_request_id', 'attempt_token',
                                      'lease_expires_at', 'consumed_at'])
               IS DISTINCT FROM
               (to_jsonb(OLD) - ARRAY['status', 'completion_request_id', 'attempt_token',
                                      'lease_expires_at', 'consumed_at']) THEN
                RAISE EXCEPTION 'upload declaration is immutable'
                    USING ERRCODE = 'check_violation';
            END IF;
            IF OLD.status IN ('consumed', 'expired') THEN
                RAISE EXCEPTION 'terminal upload session cannot change'
                    USING ERRCODE = 'check_violation';
            ELSIF OLD.status = 'pending' AND NEW.status NOT IN ('validating', 'expired') THEN
                RAISE EXCEPTION 'invalid upload session transition'
                    USING ERRCODE = 'check_violation';
            ELSIF OLD.status = 'pending' AND NEW.status = 'expired'
                  AND NEW.completion_request_id IS NOT NULL THEN
                RAISE EXCEPTION 'expiration cannot invent a completion identity'
                    USING ERRCODE = 'check_violation';
            ELSIF OLD.status = 'validating' THEN
                IF NEW.status = 'validating' AND NOT (
                    OLD.lease_expires_at <= now()
                    AND NEW.attempt_token IS DISTINCT FROM OLD.attempt_token
                ) THEN
                    RAISE EXCEPTION 'active upload lease cannot be replaced'
                        USING ERRCODE = 'check_violation';
                ELSIF NEW.status NOT IN ('validating', 'consumed', 'expired', 'pending') THEN
                    RAISE EXCEPTION 'invalid upload session transition'
                        USING ERRCODE = 'check_violation';
                ELSIF NEW.status IN ('consumed', 'expired')
                      AND NEW.completion_request_id IS DISTINCT FROM OLD.completion_request_id THEN
                    RAISE EXCEPTION 'completion identity cannot change during finalization'
                        USING ERRCODE = 'check_violation';
                ELSIF NEW.status = 'pending' AND NEW.completion_request_id IS NOT NULL THEN
                    RAISE EXCEPTION 'released upload lease must clear completion identity'
                        USING ERRCODE = 'check_violation';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$
        """
    )
    op.execute(
        "CREATE TRIGGER trg_source_upload_declaration_immutable "
        "BEFORE UPDATE ON source_upload_sessions FOR EACH ROW "
        "EXECUTE FUNCTION prevent_upload_declaration_update()"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_source_upload_declaration_immutable "
        "ON source_upload_sessions"
    )
    op.execute("DROP FUNCTION IF EXISTS prevent_upload_declaration_update()")
    op.drop_index("ix_source_upload_sessions_project_id", table_name="source_upload_sessions")
    op.drop_index("ix_source_upload_sessions_tenant_id", table_name="source_upload_sessions")
    op.drop_index("ix_source_upload_scope_status", table_name="source_upload_sessions")
    op.drop_table("source_upload_sessions")
