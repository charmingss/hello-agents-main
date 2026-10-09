"""Add event_type and outcome columns to story_bible_events.

Phase 1 — 分析字段扩展：事件增加 event_type（事迹类型）和 outcome（结果）。
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision = "0016_event_fields"
down_revision = "0015_memories"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "story_bible_events",
        sa.Column("event_type", sa.String(50), nullable=True),
    )
    op.add_column(
        "story_bible_events",
        sa.Column("outcome", sa.String(500), nullable=True),
    )

    # Recreate the guard trigger to include event_type and outcome as mutable fields
    op.execute("DROP TRIGGER IF EXISTS trg_event_guard ON story_bible_events")
    op.execute("DROP FUNCTION IF EXISTS protect_story_bible_event()")
    op.execute("""
        CREATE FUNCTION protect_story_bible_event() RETURNS trigger AS $$
        BEGIN
          IF (to_jsonb(NEW) - ARRAY[
                'title', 'description', 'occurred_at', 'location', 'participants',
                'event_type', 'outcome',
                'current_revision', 'updated_at'
              ]) IS DISTINCT FROM
             (to_jsonb(OLD) - ARRAY[
                'title', 'description', 'occurred_at', 'location', 'participants',
                'event_type', 'outcome',
                'current_revision', 'updated_at'
              ]) THEN
            RAISE EXCEPTION 'story bible event identity is immutable';
          END IF;
          IF NEW.current_revision < OLD.current_revision THEN
            RAISE EXCEPTION 'event revision cannot decrease';
          END IF;
          IF NEW.updated_at < OLD.updated_at THEN
            RAISE EXCEPTION 'event update time cannot decrease';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_event_guard
        BEFORE UPDATE OR DELETE ON story_bible_events
        FOR EACH ROW EXECUTE FUNCTION protect_story_bible_event()
    """)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_event_guard ON story_bible_events")
    op.execute("DROP FUNCTION IF EXISTS protect_story_bible_event()")
    # Restore original trigger without event_type/outcome in mutable list
    op.execute("""
        CREATE FUNCTION protect_story_bible_event() RETURNS trigger AS $$
        BEGIN
          IF (to_jsonb(NEW) - ARRAY[
                'title', 'description', 'occurred_at', 'location', 'participants',
                'current_revision', 'updated_at'
              ]) IS DISTINCT FROM
             (to_jsonb(OLD) - ARRAY[
                'title', 'description', 'occurred_at', 'location', 'participants',
                'current_revision', 'updated_at'
              ]) THEN
            RAISE EXCEPTION 'story bible event identity is immutable';
          END IF;
          IF NEW.current_revision < OLD.current_revision THEN
            RAISE EXCEPTION 'event revision cannot decrease';
          END IF;
          IF NEW.updated_at < OLD.updated_at THEN
            RAISE EXCEPTION 'event update time cannot decrease';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_event_guard
        BEFORE UPDATE OR DELETE ON story_bible_events
        FOR EACH ROW EXECUTE FUNCTION protect_story_bible_event()
    """)
    op.drop_column("story_bible_events", "outcome")
    op.drop_column("story_bible_events", "event_type")
