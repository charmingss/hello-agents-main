"""Add StoryMemory tables: per-outline writing memory items and revisions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0015_memories"
down_revision = "0014_chapters"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- story_memories --
    op.create_table(
        "story_memories",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("outline_id", sa.Uuid(), nullable=False),
        sa.Column("chapter_index", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(30), nullable=False),
        sa.Column("content", sa.String(2000), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("created_by_subject", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "outline_id",
            "id",
            name="uq_memories_scope_id",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "outline_id"],
            [
                "story_outlines.tenant_id",
                "story_outlines.project_id",
                "story_outlines.id",
            ],
            name="fk_memories_outline",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "chapter_index >= 1 AND chapter_index <= 500",
            name="ck_memories_order",
        ),
        sa.CheckConstraint("current_revision >= 0", name="ck_memories_revision"),
        sa.CheckConstraint(
            "category IN ('character', 'world', 'plot', 'foreshadowing', 'relation')",
            name="ck_memories_category",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'superseded')",
            name="ck_memories_status",
        ),
        sa.CheckConstraint("length(btrim(content)) > 0", name="ck_memories_content"),
        sa.CheckConstraint(
            "length(btrim(created_by_subject)) > 0",
            name="ck_memories_subject",
        ),
    )
    op.create_index(
        "ix_memories_outline",
        "story_memories",
        ["tenant_id", "project_id", "outline_id"],
    )

    # -- story_memory_revisions --
    op.create_table(
        "story_memory_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("outline_id", sa.Uuid(), nullable=False),
        sa.Column("memory_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "outline_id",
            "memory_id",
            "revision",
            name="uq_memory_revisions_scope_revision",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "outline_id", "memory_id"],
            [
                "story_memories.tenant_id",
                "story_memories.project_id",
                "story_memories.outline_id",
                "story_memories.id",
            ],
            name="fk_memory_revisions_memory",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_memory_revisions_revision"),
        sa.CheckConstraint("length(btrim(subject)) > 0", name="ck_memory_revisions_subject"),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name="ck_memory_revisions_json",
        ),
    )

    # -- Guard trigger: protect memory identity fields --
    op.execute("""
        CREATE FUNCTION protect_story_memory() RETURNS trigger AS $$
        BEGIN
          IF (to_jsonb(NEW) - ARRAY[
                'content', 'status', 'current_revision', 'updated_at'
              ]) IS DISTINCT FROM
             (to_jsonb(OLD) - ARRAY[
                'content', 'status', 'current_revision', 'updated_at'
              ]) THEN
            RAISE EXCEPTION 'story memory identity is immutable';
          END IF;
          IF NEW.current_revision < OLD.current_revision THEN
            RAISE EXCEPTION 'story memory revision cannot decrease';
          END IF;
          IF NEW.updated_at < OLD.updated_at THEN
            RAISE EXCEPTION 'story memory update time cannot decrease';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_story_memory_guard
        BEFORE UPDATE OR DELETE ON story_memories
        FOR EACH ROW EXECUTE FUNCTION protect_story_memory()
    """)


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM story_memories) "
        "THEN RAISE EXCEPTION 'cannot downgrade memories without losing data'; "
        "END IF; END $$"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_story_memory_guard ON story_memories")
    op.execute("DROP FUNCTION IF EXISTS protect_story_memory")
    op.drop_index("ix_memories_outline", table_name="story_memories")
    op.drop_table("story_memory_revisions")
    op.drop_table("story_memories")
