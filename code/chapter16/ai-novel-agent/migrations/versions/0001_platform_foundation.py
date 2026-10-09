"""Create the tenant-scoped platform authority tables.

Revision ID: 0001_platform_foundation
Revises:
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0001_platform_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "tenants",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("slug", sa.String(length=80), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("slug"),
    )
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("creation_request_id", sa.String(length=100), nullable=False),
        sa.Column("slug", sa.String(length=100), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("language", sa.String(length=20), server_default="zh-CN", nullable=False),
        sa.Column("status", sa.String(length=30), server_default="active", nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "creation_request_id", name="uq_projects_tenant_creation_request"
        ),
        sa.UniqueConstraint("tenant_id", "id", name="uq_projects_tenant_id"),
        sa.UniqueConstraint("tenant_id", "slug", name="uq_projects_tenant_slug"),
    )
    op.create_index(op.f("ix_projects_tenant_id"), "projects", ["tenant_id"], unique=False)
    op.create_table(
        "story_branches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("is_default", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_story_branches_tenant_project",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "name", name="uq_story_branches_tenant_project_name"
        ),
    )
    op.create_index(
        op.f("ix_story_branches_project_id"), "story_branches", ["project_id"], unique=False
    )
    op.create_index(
        op.f("ix_story_branches_tenant_id"), "story_branches", ["tenant_id"], unique=False
    )
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("aggregate_type", sa.String(length=100), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=150), nullable=False),
        sa.Column("aggregate_version", sa.Integer(), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("retry_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_outbox_events_tenant_project",
        ),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "aggregate_type",
            "aggregate_id",
            "aggregate_version",
            "event_type",
            name="uq_outbox_events_tenant_aggregate_version_event",
        ),
    )
    op.create_index(
        op.f("ix_outbox_events_project_id"), "outbox_events", ["project_id"], unique=False
    )
    op.create_index(
        op.f("ix_outbox_events_tenant_id"), "outbox_events", ["tenant_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_outbox_events_tenant_id"), table_name="outbox_events")
    op.drop_index(op.f("ix_outbox_events_project_id"), table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index(op.f("ix_story_branches_tenant_id"), table_name="story_branches")
    op.drop_index(op.f("ix_story_branches_project_id"), table_name="story_branches")
    op.drop_table("story_branches")
    op.drop_index(op.f("ix_projects_tenant_id"), table_name="projects")
    op.drop_table("projects")
    op.drop_table("tenants")
