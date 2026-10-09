"""Add StyleProfile tables: per-project narrative style profile with revisions."""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_style_profiles"
down_revision = "0011_story_graph"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # -- story_bible_style_profiles --
    op.create_table(
        "story_bible_style_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("story_bible_id", sa.Uuid(), nullable=False),
        sa.Column("narrative_voice", sa.String(200), nullable=True),
        sa.Column("tense", sa.String(50), nullable=True),
        sa.Column("pacing", sa.String(200), nullable=True),
        sa.Column("tone", sa.String(200), nullable=True),
        sa.Column("vocabulary_level", sa.String(200), nullable=True),
        sa.Column("sentence_structure", sa.String(200), nullable=True),
        sa.Column("notes", sa.String(2000), nullable=True),
        sa.Column("current_revision", sa.Integer(), nullable=False),
        sa.Column("created_by_subject", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "id",
            name="uq_style_profiles_scope_id",
        ),
        sa.UniqueConstraint(
            "tenant_id", "project_id",
            name="uq_style_profiles_scope_project",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id"],
            ["story_bibles.tenant_id", "story_bibles.project_id", "story_bibles.id"],
            name="fk_style_profiles_bible",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_style_profiles_project",
        ),
        sa.CheckConstraint("current_revision >= 0", name="ck_style_profiles_revision"),
        sa.CheckConstraint(
            "length(btrim(created_by_subject)) > 0",
            name="ck_style_profiles_text",
        ),
    )

    # -- story_bible_style_profile_revisions --
    op.create_table(
        "story_bible_style_profile_revisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("style_profile_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("subject", sa.String(255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "tenant_id", "project_id", "style_profile_id", "revision",
            name="uq_style_profile_revisions_scope_revision",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id", "project_id", "style_profile_id"],
            [
                "story_bible_style_profiles.tenant_id",
                "story_bible_style_profiles.project_id",
                "story_bible_style_profiles.id",
            ],
            name="fk_style_profile_revisions_profile",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint("revision >= 0", name="ck_style_profile_revisions_revision"),
        sa.CheckConstraint(
            "length(btrim(subject)) > 0", name="ck_style_profile_revisions_subject"
        ),
        sa.CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name="ck_style_profile_revisions_json",
        ),
    )

    # -- Guard trigger: protect style profile identity fields --
    op.execute("""
        CREATE FUNCTION protect_style_profile() RETURNS trigger AS $$
        BEGIN
          IF (to_jsonb(NEW) - ARRAY[
                'narrative_voice', 'tense', 'pacing', 'tone',
                'vocabulary_level', 'sentence_structure', 'notes',
                'current_revision', 'updated_at'
              ]) IS DISTINCT FROM
             (to_jsonb(OLD) - ARRAY[
                'narrative_voice', 'tense', 'pacing', 'tone',
                'vocabulary_level', 'sentence_structure', 'notes',
                'current_revision', 'updated_at'
              ]) THEN
            RAISE EXCEPTION 'style profile identity is immutable';
          END IF;
          IF NEW.current_revision < OLD.current_revision THEN
            RAISE EXCEPTION 'style profile revision cannot decrease';
          END IF;
          IF NEW.updated_at < OLD.updated_at THEN
            RAISE EXCEPTION 'style profile update time cannot decrease';
          END IF;
          RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
    """)
    op.execute("""
        CREATE TRIGGER trg_style_profile_guard
        BEFORE UPDATE OR DELETE ON story_bible_style_profiles
        FOR EACH ROW EXECUTE FUNCTION protect_style_profile()
    """)


def downgrade() -> None:
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM story_bible_style_profiles) "
        "THEN RAISE EXCEPTION 'cannot downgrade style profiles without losing data'; "
        "END IF; END $$"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_style_profile_guard ON story_bible_style_profiles"
    )
    op.execute("DROP FUNCTION IF EXISTS protect_style_profile")
    op.drop_table("story_bible_style_profile_revisions")
    op.drop_table("story_bible_style_profiles")
