"""Add StoryOutline tables: per-project novel outline with chapters and revisions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0013_outlines"
down_revision = "0012_style_profiles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- story_outlines --
    op.create_table(
        "story_outlines",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("premise", sa.String(2000), nullable=False),
        sa.Column("target_chapters", sa.Integer(), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("created_by_subject", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "id",
            name="uq_outlines_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            name="uq_outlines_scope_project",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_outlines_project",
        ),
        sa.CheckConstraint("current_revision >= 0", name="ck_outlines_revision"),
        sa.CheckConstraint(
            "length(btrim(created_by_subject)) > 0",
            name="ck_outlines_text",
        ),
        sa.CheckConstraint(
            "target_chapters >= 1 AND target_chapters <= 500",
            name="ck_outlines_target_chapters",
        ),
    )

    # -- story_outline_revisions --
    op.create_table(
        "story_outline_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("outline_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "outline_id",
            "revision",
            name="uq_outline_revisions_scope_revision",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "outline_id"],
            [
                "story_outlines.tenant_id",
                "story_outlines.project_id",
                "story_outlines.id",
            ],
            name="fk_outline_revisions_outline",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_outline_revisions_revision"),
        sa.CheckConstraint("length(btrim(subject)) > 0", name="ck_outline_revisions_subject"),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name="ck_outline_revisions_json",
        ),
    )

    # -- story_outline_chapters --
    op.create_table(
        "story_outline_chapters",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("outline_id", sa.Uuid(), nullable=False),
        sa.Column("order_index", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("summary", sa.String(2000), nullable=False),
        sa.Column("key_events", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("notes", sa.String(2000), nullable=True),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "outline_id",
            "id",
            name="uq_outline_chapters_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "outline_id",
            "order_index",
            name="uq_outline_chapters_scope_order",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "outline_id"],
            [
                "story_outlines.tenant_id",
                "story_outlines.project_id",
                "story_outlines.id",
            ],
            name="fk_outline_chapters_outline",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_outline_chapters_project",
        ),
        sa.CheckConstraint(
            "order_index >= 1 AND order_index <= 500",
            name="ck_outline_chapters_order",
        ),
        sa.CheckConstraint("current_revision >= 0", name="ck_outline_chapters_revision"),
        sa.CheckConstraint(
            "jsonb_typeof(key_events) = 'array'",
            name="ck_outline_chapters_json",
        ),
    )
    op.create_index(
        "ix_outline_chapters_outline",
        "story_outline_chapters",
        ["tenant_id", "project_id", "outline_id"],
    )

    # -- story_outline_chapter_revisions --
    op.create_table(
        "story_outline_chapter_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("outline_id", sa.Uuid(), nullable=False),
        sa.Column("chapter_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "outline_id",
            "chapter_id",
            "revision",
            name="uq_outline_chapter_revisions_scope_revision",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "outline_id", "chapter_id"],
            [
                "story_outline_chapters.tenant_id",
                "story_outline_chapters.project_id",
                "story_outline_chapters.outline_id",
                "story_outline_chapters.id",
            ],
            name="fk_outline_chapter_revisions_chapter",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_outline_chapter_revisions_revision"),
        sa.CheckConstraint(
            "length(btrim(subject)) > 0", name="ck_outline_chapter_revisions_subject"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name="ck_outline_chapter_revisions_json",
        ),
    )

    # -- Guard trigger: protect outline identity fields --
    op.execute("""
        CREATE FUNCTION protect_story_outline() RETURNS trigger AS $$
        BEGIN
          IF (to_jsonb(NEW) - ARRAY[
                'title', 'premise', 'target_chapters',
                'current_revision', 'updated_at'
              ]) IS DISTINCT FROM
             (to_jsonb(OLD) - ARRAY[
                'title', 'premise', 'target_chapters',
                'current_revision', 'updated_at'
              ]) THEN
            RAISE EXCEPTION 'story outline identity is immutable';
          END IF;
          IF NEW.current_revision < OLD.current_revision THEN
            RAISE EXCEPTION 'story outline revision cannot decrease';
          END IF;
          IF NEW.updated_at < OLD.updated_at THEN
            RAISE EXCEPTION 'story outline update time cannot decrease';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_story_outline_guard
        BEFORE UPDATE OR DELETE ON story_outlines
        FOR EACH ROW EXECUTE FUNCTION protect_story_outline()
    """)

    # -- Guard trigger: protect chapter identity fields --
    op.execute("""
        CREATE FUNCTION protect_story_outline_chapter() RETURNS trigger AS $$
        BEGIN
          IF (to_jsonb(NEW) - ARRAY[
                'title', 'summary', 'key_events', 'notes',
                'current_revision', 'updated_at'
              ]) IS DISTINCT FROM
             (to_jsonb(OLD) - ARRAY[
                'title', 'summary', 'key_events', 'notes',
                'current_revision', 'updated_at'
              ]) THEN
            RAISE EXCEPTION 'story outline chapter identity is immutable';
          END IF;
          IF NEW.current_revision < OLD.current_revision THEN
            RAISE EXCEPTION 'story outline chapter revision cannot decrease';
          END IF;
          IF NEW.updated_at < OLD.updated_at THEN
            RAISE EXCEPTION 'story outline chapter update time cannot decrease';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_story_outline_chapter_guard
        BEFORE UPDATE OR DELETE ON story_outline_chapters
        FOR EACH ROW EXECUTE FUNCTION protect_story_outline_chapter()
    """)


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM story_outlines) "
        "THEN RAISE EXCEPTION 'cannot downgrade outlines without losing data'; "
        "END IF; END $$"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_story_outline_chapter_guard ON story_outline_chapters")
    op.execute("DROP FUNCTION IF EXISTS protect_story_outline_chapter")
    op.execute("DROP TRIGGER IF EXISTS trg_story_outline_guard ON story_outlines")
    op.execute("DROP FUNCTION IF EXISTS protect_story_outline")
    op.drop_index("ix_outline_chapters_outline", table_name="story_outline_chapters")
    op.drop_table("story_outline_chapter_revisions")
    op.drop_table("story_outline_chapters")
    op.drop_table("story_outline_revisions")
    op.drop_table("story_outlines")
