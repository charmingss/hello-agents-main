"""Add StoryChapter tables: per-outline generated chapter content and revisions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0014_chapters"
down_revision = "0013_outlines"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- story_chapters --
    op.create_table(
        "story_chapters",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("outline_id", sa.Uuid(), nullable=False),
        sa.Column("chapter_index", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("summary", sa.String(2000), nullable=False),
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
            name="uq_chapters_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id",
            "project_id",
            "outline_id",
            "chapter_index",
            name="uq_chapters_scope_order",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "outline_id"],
            [
                "story_outlines.tenant_id",
                "story_outlines.project_id",
                "story_outlines.id",
            ],
            name="fk_chapters_outline",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "chapter_index >= 1 AND chapter_index <= 500",
            name="ck_chapters_order",
        ),
        sa.CheckConstraint("current_revision >= 0", name="ck_chapters_revision"),
        sa.CheckConstraint("length(btrim(title)) > 0", name="ck_chapters_title"),
        sa.CheckConstraint(
            "length(btrim(created_by_subject)) > 0",
            name="ck_chapters_subject",
        ),
    )
    op.create_index(
        "ix_chapters_outline",
        "story_chapters",
        ["tenant_id", "project_id", "outline_id"],
    )

    # -- story_chapter_revisions --
    op.create_table(
        "story_chapter_revisions",
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
            name="uq_chapter_revisions_scope_revision",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "outline_id", "chapter_id"],
            [
                "story_chapters.tenant_id",
                "story_chapters.project_id",
                "story_chapters.outline_id",
                "story_chapters.id",
            ],
            name="fk_chapter_revisions_chapter",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_chapter_revisions_revision"),
        sa.CheckConstraint("length(btrim(subject)) > 0", name="ck_chapter_revisions_subject"),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name="ck_chapter_revisions_json",
        ),
    )

    # -- Guard trigger: protect chapter identity fields --
    op.execute("""
        CREATE FUNCTION protect_story_chapter() RETURNS trigger AS $$
        BEGIN
          IF (to_jsonb(NEW) - ARRAY[
                'title', 'content', 'summary',
                'current_revision', 'updated_at'
              ]) IS DISTINCT FROM
             (to_jsonb(OLD) - ARRAY[
                'title', 'content', 'summary',
                'current_revision', 'updated_at'
              ]) THEN
            RAISE EXCEPTION 'story chapter identity is immutable';
          END IF;
          IF NEW.current_revision < OLD.current_revision THEN
            RAISE EXCEPTION 'story chapter revision cannot decrease';
          END IF;
          IF NEW.updated_at < OLD.updated_at THEN
            RAISE EXCEPTION 'story chapter update time cannot decrease';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_story_chapter_guard
        BEFORE UPDATE OR DELETE ON story_chapters
        FOR EACH ROW EXECUTE FUNCTION protect_story_chapter()
    """)


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM story_chapters) "
        "THEN RAISE EXCEPTION 'cannot downgrade chapters without losing data'; "
        "END IF; END $$"
    )
    op.execute("DROP TRIGGER IF EXISTS trg_story_chapter_guard ON story_chapters")
    op.execute("DROP FUNCTION IF EXISTS protect_story_chapter")
    op.drop_index("ix_chapters_outline", table_name="story_chapters")
    op.drop_table("story_chapter_revisions")
    op.drop_table("story_chapters")
