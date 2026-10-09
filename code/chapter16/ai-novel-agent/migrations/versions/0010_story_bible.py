"""Add Story Bible: canonical characters, world entries, and foreshadowing."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_story_bible"
down_revision = "0009_full_analysis_jobs"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- story_bibles (parent) --
    op.create_table(
        "story_bibles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("created_by_subject", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tenant_id", "project_id", name="uq_story_bibles_scope_project"),
        sa.UniqueConstraint("tenant_id", "project_id", "id", name="uq_story_bibles_scope_id"),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_story_bibles_project",
        ),
        sa.CheckConstraint(
            "length(btrim(created_by_subject)) > 0",
            name="ck_story_bibles_text",
        ),
    )

    # -- story_bible_characters --
    op.create_table(
        "story_bible_characters",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("story_bible_id", sa.Uuid(), nullable=False),
        sa.Column("character_key", sa.String(120), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("aliases", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("description", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("traits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("background", sa.String(2000), nullable=True),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("source_document_id", sa.Uuid(), nullable=True),
        sa.Column("analysis_id", sa.Uuid(), nullable=True),
        sa.Column("created_by_subject", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "story_bible_id", "id",
            name="uq_characters_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "story_bible_id", "character_key",
            name="uq_characters_scope_key",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id"],
            ["story_bibles.tenant_id", "story_bibles.project_id", "story_bibles.id"],
            name="fk_characters_story_bible",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_characters_project",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "source_document_id", "analysis_id"],
            [
                "source_analysis_records.tenant_id",
                "source_analysis_records.project_id",
                "source_analysis_records.source_document_id",
                "source_analysis_records.id",
            ],
            name="fk_characters_source_analysis",
            ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "length(btrim(name)) > 0 AND length(btrim(created_by_subject)) > 0",
            name="ck_characters_text",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(aliases) = 'array' AND jsonb_array_length(aliases) <= 10 "
            "AND jsonb_typeof(description) = 'array' AND jsonb_array_length(description) <= 10 "
            "AND jsonb_typeof(traits) = 'array' AND jsonb_array_length(traits) <= 10",
            name="ck_characters_json",
        ),
        sa.CheckConstraint("current_revision >= 0", name="ck_characters_revision"),
        sa.CheckConstraint(
            "(source_document_id IS NULL) = (analysis_id IS NULL)",
            name="ck_characters_source_pair",
        ),
    )
    op.create_index(
        "ix_characters_bible", "story_bible_characters",
        ["tenant_id", "project_id", "story_bible_id"],
    )

    # -- story_bible_character_revisions --
    op.create_table(
        "story_bible_character_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("story_bible_id", sa.Uuid(), nullable=False),
        sa.Column("character_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "story_bible_id", "character_id", "revision",
            name="uq_character_revisions_scope_revision",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id", "character_id"],
            [
                "story_bible_characters.tenant_id",
                "story_bible_characters.project_id",
                "story_bible_characters.story_bible_id",
                "story_bible_characters.id",
            ],
            name="fk_character_revisions_character",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_character_revisions_revision"),
        sa.CheckConstraint("length(btrim(subject)) > 0", name="ck_character_revisions_subject"),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name="ck_character_revisions_json",
        ),
    )

    # -- story_bible_world_entries --
    op.create_table(
        "story_bible_world_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("story_bible_id", sa.Uuid(), nullable=False),
        sa.Column("entry_type", sa.String(20), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("aliases", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("description", sa.String(2000), nullable=False),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("created_by_subject", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "story_bible_id", "id",
            name="uq_world_entries_scope_id",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id"],
            ["story_bibles.tenant_id", "story_bibles.project_id", "story_bibles.id"],
            name="fk_world_entries_story_bible",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_world_entries_project",
        ),
        sa.CheckConstraint(
            "entry_type IN ('location', 'organization', 'rule', 'item', 'other')",
            name="ck_world_entries_type",
        ),
        sa.CheckConstraint(
            "length(btrim(name)) > 0 AND length(btrim(created_by_subject)) > 0",
            name="ck_world_entries_text",
        ),
        sa.CheckConstraint(
            "jsonb_typeof(aliases) = 'array' AND jsonb_array_length(aliases) <= 10",
            name="ck_world_entries_json",
        ),
        sa.CheckConstraint("current_revision >= 0", name="ck_world_entries_revision"),
    )
    op.create_index(
        "ix_world_entries_bible", "story_bible_world_entries",
        ["tenant_id", "project_id", "story_bible_id"],
    )

    # -- story_bible_world_entry_revisions --
    op.create_table(
        "story_bible_world_entry_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("story_bible_id", sa.Uuid(), nullable=False),
        sa.Column("entry_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "story_bible_id", "entry_id", "revision",
            name="uq_world_entry_revisions_scope_revision",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id", "entry_id"],
            [
                "story_bible_world_entries.tenant_id",
                "story_bible_world_entries.project_id",
                "story_bible_world_entries.story_bible_id",
                "story_bible_world_entries.id",
            ],
            name="fk_world_entry_revisions_entry",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_world_entry_revisions_revision"),
        sa.CheckConstraint("length(btrim(subject)) > 0", name="ck_world_entry_revisions_subject"),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name="ck_world_entry_revisions_json",
        ),
    )

    # -- story_bible_foreshadowing --
    op.create_table(
        "story_bible_foreshadowing",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("story_bible_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("description", sa.String(2000), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("resolution", sa.String(2000), nullable=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("created_by_subject", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "story_bible_id", "id",
            name="uq_foreshadowing_scope_id",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id"],
            ["story_bibles.tenant_id", "story_bibles.project_id", "story_bibles.id"],
            name="fk_foreshadowing_story_bible",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_foreshadowing_project",
        ),
        sa.CheckConstraint(
            "status IN ('planted', 'resolved')",
            name="ck_foreshadowing_status",
        ),
        sa.CheckConstraint(
            "length(btrim(title)) > 0 AND length(btrim(description)) > 0 "
            "AND length(btrim(created_by_subject)) > 0",
            name="ck_foreshadowing_text",
        ),
        sa.CheckConstraint(
            "(status = 'resolved') = (resolution IS NOT NULL AND resolved_at IS NOT NULL)",
            name="ck_foreshadowing_resolved",
        ),
        sa.CheckConstraint("current_revision >= 0", name="ck_foreshadowing_revision"),
    )
    op.create_index(
        "ix_foreshadowing_bible", "story_bible_foreshadowing",
        ["tenant_id", "project_id", "story_bible_id", "status"],
    )

    # -- story_bible_foreshadowing_revisions --
    op.create_table(
        "story_bible_foreshadowing_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("story_bible_id", sa.Uuid(), nullable=False),
        sa.Column("foreshadowing_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "story_bible_id", "foreshadowing_id", "revision",
            name="uq_foreshadowing_revisions_scope_revision",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id", "foreshadowing_id"],
            [
                "story_bible_foreshadowing.tenant_id",
                "story_bible_foreshadowing.project_id",
                "story_bible_foreshadowing.story_bible_id",
                "story_bible_foreshadowing.id",
            ],
            name="fk_foreshadowing_revisions_entry",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_foreshadowing_revisions_revision"),
        sa.CheckConstraint("length(btrim(subject)) > 0", name="ck_foreshadowing_revisions_subject"),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name="ck_foreshadowing_revisions_json",
        ),
    )

    # -- Guard trigger: protect story_bible immutable identity fields --
    op.execute("""
        CREATE FUNCTION protect_story_bible() RETURNS trigger AS $$
        BEGIN
          IF (to_jsonb(NEW) - ARRAY['updated_at']) IS DISTINCT FROM
             (to_jsonb(OLD) - ARRAY['updated_at']) THEN
            RAISE EXCEPTION 'story bible identity is immutable';
          END IF;
          IF NEW.updated_at < OLD.updated_at THEN
            RAISE EXCEPTION 'story bible update time cannot decrease';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_story_bible_guard
        BEFORE UPDATE OR DELETE ON story_bibles
        FOR EACH ROW EXECUTE FUNCTION protect_story_bible()
    """)

    # -- Guard trigger: protect character identity fields --
    op.execute("""
        CREATE FUNCTION protect_story_bible_character() RETURNS trigger AS $$
        BEGIN
          IF (to_jsonb(NEW) - ARRAY[
                'name', 'aliases', 'description', 'traits', 'background',
                'current_revision', 'updated_at'
              ]) IS DISTINCT FROM
             (to_jsonb(OLD) - ARRAY[
                'name', 'aliases', 'description', 'traits', 'background',
                'current_revision', 'updated_at'
              ]) THEN
            RAISE EXCEPTION 'story bible character identity is immutable';
          END IF;
          IF NEW.current_revision < OLD.current_revision THEN
            RAISE EXCEPTION 'character revision cannot decrease';
          END IF;
          IF NEW.updated_at < OLD.updated_at THEN
            RAISE EXCEPTION 'character update time cannot decrease';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_character_guard
        BEFORE UPDATE OR DELETE ON story_bible_characters
        FOR EACH ROW EXECUTE FUNCTION protect_story_bible_character()
    """)

    # -- Guard trigger: protect world entry identity fields --
    op.execute("""
        CREATE FUNCTION protect_story_bible_world_entry() RETURNS trigger AS $$
        BEGIN
          IF (to_jsonb(NEW) - ARRAY[
                'name', 'aliases', 'description', 'current_revision', 'updated_at'
              ]) IS DISTINCT FROM
             (to_jsonb(OLD) - ARRAY[
                'name', 'aliases', 'description', 'current_revision', 'updated_at'
              ]) THEN
            RAISE EXCEPTION 'story bible world entry identity is immutable';
          END IF;
          IF NEW.current_revision < OLD.current_revision THEN
            RAISE EXCEPTION 'world entry revision cannot decrease';
          END IF;
          IF NEW.updated_at < OLD.updated_at THEN
            RAISE EXCEPTION 'world entry update time cannot decrease';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_world_entry_guard
        BEFORE UPDATE OR DELETE ON story_bible_world_entries
        FOR EACH ROW EXECUTE FUNCTION protect_story_bible_world_entry()
    """)

    # -- Guard trigger: protect foreshadowing identity fields --
    op.execute("""
        CREATE FUNCTION protect_story_bible_foreshadowing() RETURNS trigger AS $$
        BEGIN
          IF (to_jsonb(NEW) - ARRAY[
                'status', 'resolution', 'resolved_at', 'current_revision', 'updated_at'
              ]) IS DISTINCT FROM
             (to_jsonb(OLD) - ARRAY[
                'status', 'resolution', 'resolved_at', 'current_revision', 'updated_at'
              ]) THEN
            RAISE EXCEPTION 'story bible foreshadowing identity is immutable';
          END IF;
          IF NEW.status <> OLD.status AND NOT (
               (OLD.status = 'planted' AND NEW.status = 'resolved')
          ) THEN
            RAISE EXCEPTION 'invalid foreshadowing status transition';
          END IF;
          IF NEW.current_revision < OLD.current_revision THEN
            RAISE EXCEPTION 'foreshadowing revision cannot decrease';
          END IF;
          IF NEW.updated_at < OLD.updated_at THEN
            RAISE EXCEPTION 'foreshadowing update time cannot decrease';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_foreshadowing_guard
        BEFORE UPDATE OR DELETE ON story_bible_foreshadowing
        FOR EACH ROW EXECUTE FUNCTION protect_story_bible_foreshadowing()
    """)


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM story_bible_characters) "
        "OR EXISTS (SELECT 1 FROM story_bible_world_entries) "
        "OR EXISTS (SELECT 1 FROM story_bible_foreshadowing) THEN "
        "RAISE EXCEPTION 'cannot downgrade story bible without losing data'; "
        "END IF; END $$"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_foreshadowing_guard ON story_bible_foreshadowing"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_story_bible_foreshadowing")
    op.execute("DROP TRIGGER IF EXISTS trg_world_entry_guard ON story_bible_world_entries")
    op.execute("DROP FUNCTION IF EXISTS protect_story_bible_world_entry")
    op.execute("DROP TRIGGER IF EXISTS trg_character_guard ON story_bible_characters")
    op.execute("DROP FUNCTION IF EXISTS protect_story_bible_character")
    op.execute("DROP TRIGGER IF EXISTS trg_story_bible_guard ON story_bibles")
    op.execute("DROP FUNCTION IF EXISTS protect_story_bible")
    op.drop_table("story_bible_foreshadowing_revisions")
    op.drop_index("ix_foreshadowing_bible", table_name="story_bible_foreshadowing")
    op.drop_table("story_bible_foreshadowing")
    op.drop_table("story_bible_world_entry_revisions")
    op.drop_index("ix_world_entries_bible", table_name="story_bible_world_entries")
    op.drop_table("story_bible_world_entries")
    op.drop_table("story_bible_character_revisions")
    op.drop_index("ix_characters_bible", table_name="story_bible_characters")
    op.drop_table("story_bible_characters")
    op.drop_table("story_bibles")
