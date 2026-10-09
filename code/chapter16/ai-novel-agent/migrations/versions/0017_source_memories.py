"""Add source_document_id to story_memories for source-based memory extraction.

Phase 2 — 范文记忆提取：记忆可来源于导入的范文（source_document）而非仅限章节。
- chapter_index 改为可空（范文记忆无章节序号）
- 新增 source_document_id 可空列（范文记忆标记来源）
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0017_source_memories"
down_revision = "0016_event_fields"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # Add source_document_id column (nullable, no FK — source_documents uses composite FK)
    op.add_column(
        "story_memories",
        sa.Column("source_document_id", sa.Uuid(), nullable=True),
    )

    # Add index for source-based queries
    op.create_index(
        "ix_memories_source",
        "story_memories",
        ["tenant_id", "project_id", "source_document_id"],
        postgresql_where=sa.text("source_document_id IS NOT NULL"),
    )

    # Relax chapter_index constraint: allow NULL for source-based memories
    op.drop_constraint("ck_memories_order", "story_memories", type_="check")
    op.create_check_constraint(
        "ck_memories_order",
        "story_memories",
        "chapter_index IS NULL OR (chapter_index >= 1 AND chapter_index <= 500)",
    )

    # Recreate guard trigger to include source_document_id as immutable
    op.execute("DROP TRIGGER IF EXISTS trg_story_memory_guard ON story_memories")
    op.execute("DROP FUNCTION IF EXISTS protect_story_memory()")
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
    op.execute("DROP TRIGGER IF EXISTS trg_story_memory_guard ON story_memories")
    op.execute("DROP FUNCTION IF EXISTS protect_story_memory()")

    # Restore original constraint (chapter_index required 1-500)
    op.drop_constraint("ck_memories_order", "story_memories", type_="check")
    op.create_check_constraint(
        "ck_memories_order",
        "story_memories",
        "chapter_index >= 1 AND chapter_index <= 500",
    )

    # Restore original guard trigger
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

    op.drop_index("ix_memories_source", table_name="story_memories")
    op.drop_column("story_memories", "source_document_id")
