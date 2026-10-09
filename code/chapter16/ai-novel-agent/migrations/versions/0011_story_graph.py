"""Add Story Graph: canonical relations, events, and event participants."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_story_graph"
down_revision = "0010_story_bible"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- story_bible_relations (parent) --
    op.create_table(
        "story_bible_relations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("story_bible_id", sa.Uuid(), nullable=False),
        sa.Column("relation_key", sa.String(120), nullable=False),
        sa.Column("relation_type", sa.String(50), nullable=False),
        sa.Column("from_character_id", sa.Uuid(), nullable=False),
        sa.Column("to_character_id", sa.Uuid(), nullable=False),
        sa.Column("description", sa.String(500), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("created_by_subject", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "story_bible_id", "id",
            name="uq_relations_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "story_bible_id", "relation_key",
            name="uq_relations_scope_key",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id"],
            ["story_bibles.tenant_id", "story_bibles.project_id", "story_bibles.id"],
            name="fk_relations_story_bible",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id", "from_character_id"],
            [
                "story_bible_characters.tenant_id",
                "story_bible_characters.project_id",
                "story_bible_characters.story_bible_id",
                "story_bible_characters.id",
            ],
            name="fk_relations_from_character",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id", "to_character_id"],
            [
                "story_bible_characters.tenant_id",
                "story_bible_characters.project_id",
                "story_bible_characters.story_bible_id",
                "story_bible_characters.id",
            ],
            name="fk_relations_to_character",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_relations_project",
        ),
        sa.CheckConstraint(
            "length(btrim(relation_type)) > 0 AND length(btrim(description)) > 0 "
            "AND length(btrim(created_by_subject)) > 0",
            name="ck_relations_text",
        ),
        sa.CheckConstraint(
            "from_character_id <> to_character_id",
            name="ck_relations_distinct",
        ),
        sa.CheckConstraint("current_revision >= 0", name="ck_relations_revision"),
    )
    op.create_index(
        "ix_relations_bible", "story_bible_relations",
        ["tenant_id", "project_id", "story_bible_id"],
    )

    # -- story_bible_relation_revisions --
    op.create_table(
        "story_bible_relation_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("story_bible_id", sa.Uuid(), nullable=False),
        sa.Column("relation_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "story_bible_id", "relation_id", "revision",
            name="uq_relation_revisions_scope_revision",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id", "relation_id"],
            [
                "story_bible_relations.tenant_id",
                "story_bible_relations.project_id",
                "story_bible_relations.story_bible_id",
                "story_bible_relations.id",
            ],
            name="fk_relation_revisions_relation",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_relation_revisions_revision"),
        sa.CheckConstraint("length(btrim(subject)) > 0", name="ck_relation_revisions_subject"),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name="ck_relation_revisions_json",
        ),
    )

    # -- story_bible_events --
    op.create_table(
        "story_bible_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("story_bible_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.String(2000), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("location", sa.String(500), nullable=True),
        sa.Column("participants", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("created_by_subject", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "story_bible_id", "id",
            name="uq_events_scope_id",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id"],
            ["story_bibles.tenant_id", "story_bibles.project_id", "story_bibles.id"],
            name="fk_events_story_bible",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_events_project",
        ),
        sa.CheckConstraint(
            "length(btrim(title)) > 0 AND length(btrim(description)) > 0 "
            "AND length(btrim(created_by_subject)) > 0",
            name="ck_events_text",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(participants) = 'array' "
            "AND jsonb_array_length(participants) <= 100",
            name="ck_events_participants",
        ),
        sa.CheckConstraint("current_revision >= 0", name="ck_events_revision"),
    )
    op.create_index(
        "ix_events_bible", "story_bible_events",
        ["tenant_id", "project_id", "story_bible_id"],
    )

    # -- story_bible_event_revisions --
    op.create_table(
        "story_bible_event_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("story_bible_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "story_bible_id", "event_id", "revision",
            name="uq_event_revisions_scope_revision",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id", "event_id"],
            [
                "story_bible_events.tenant_id",
                "story_bible_events.project_id",
                "story_bible_events.story_bible_id",
                "story_bible_events.id",
            ],
            name="fk_event_revisions_event",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_event_revisions_revision"),
        sa.CheckConstraint("length(btrim(subject)) > 0", name="ck_event_revisions_subject"),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name="ck_event_revisions_json",
        ),
    )

    # -- Guard trigger: protect relation identity fields --
    op.execute("""
        CREATE FUNCTION protect_story_bible_relation() RETURNS trigger AS $$
        BEGIN
          IF (to_jsonb(NEW) - ARRAY[
                'relation_type', 'description', 'current_revision', 'updated_at'
              ]) IS DISTINCT FROM
             (to_jsonb(OLD) - ARRAY[
                'relation_type', 'description', 'current_revision', 'updated_at'
              ]) THEN
            RAISE EXCEPTION 'story bible relation identity is immutable';
          END IF;
          IF NEW.current_revision < OLD.current_revision THEN
            RAISE EXCEPTION 'relation revision cannot decrease';
          END IF;
          IF NEW.updated_at < OLD.updated_at THEN
            RAISE EXCEPTION 'relation update time cannot decrease';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_relation_guard
        BEFORE UPDATE OR DELETE ON story_bible_relations
        FOR EACH ROW EXECUTE FUNCTION protect_story_bible_relation()
    """)

    # -- Guard trigger: protect event identity fields --
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


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM story_bible_relations) "
        "OR EXISTS (SELECT 1 FROM story_bible_events) THEN "
        "RAISE EXCEPTION 'cannot downgrade story graph without losing data'; "
        "END IF; END $$"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_event_guard ON story_bible_events"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_story_bible_event")
    op.execute(
        "DROP TRIGGER IF EXISTS trg_relation_guard ON story_bible_relations"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_story_bible_relation")
    op.drop_table("story_bible_event_revisions")
    op.drop_index("ix_events_bible", table_name="story_bible_events")
    op.drop_table("story_bible_events")
    op.drop_table("story_bible_relation_revisions")
    op.drop_index("ix_relations_bible", table_name="story_bible_relations")
    op.drop_table("story_bible_relations")