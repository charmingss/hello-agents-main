"""Story Bible: canonical character settings, world entries, and foreshadowing."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from novel_agent.db.base import Base


class StoryBible(Base):
    """One per project, auto-created. Owns the canonical setting registry."""

    __tablename__ = "story_bibles"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_story_bibles_project",
        ),
        UniqueConstraint("tenant_id", "project_id", name="uq_story_bibles_scope_project"),
        UniqueConstraint("tenant_id", "project_id", "id", name="uq_story_bibles_scope_id"),
        CheckConstraint(
            "length(btrim(created_by_subject)) > 0",
            name="ck_story_bibles_text",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    created_by_subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Character(Base):
    """One canonical character in the story bible."""

    __tablename__ = "story_bible_characters"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id"],
            ["story_bibles.tenant_id", "story_bibles.project_id", "story_bibles.id"],
            name="fk_characters_story_bible",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_characters_project",
        ),
        ForeignKeyConstraint(
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
        UniqueConstraint(
            "tenant_id", "project_id", "story_bible_id", "id",
            name="uq_characters_scope_id",
        ),
        UniqueConstraint(
            "tenant_id", "project_id", "story_bible_id", "character_key",
            name="uq_characters_scope_key",
        ),
        CheckConstraint(
            "length(btrim(name)) > 0 AND length(btrim(created_by_subject)) > 0",
            name="ck_characters_text",
        ),
        CheckConstraint(
            "jsonb_typeof(aliases) = 'array' AND jsonb_array_length(aliases) <= 10 "
            "AND jsonb_typeof(description) = 'array' AND jsonb_array_length(description) <= 10 "
            "AND jsonb_typeof(traits) = 'array' AND jsonb_array_length(traits) <= 10",
            name="ck_characters_json",
        ),
        CheckConstraint(
            "current_revision >= 0",
            name="ck_characters_revision",
        ),
        CheckConstraint(
            "(source_document_id IS NULL) = (analysis_id IS NULL)",
            name="ck_characters_source_pair",
        ),
        Index(
            "ix_characters_bible", "tenant_id", "project_id", "story_bible_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    story_bible_id: Mapped[UUID] = mapped_column(Uuid)
    character_key: Mapped[str] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(120))
    aliases: Mapped[list[str]] = mapped_column(JSONB)
    description: Mapped[list[str]] = mapped_column(JSONB)
    traits: Mapped[list[str]] = mapped_column(JSONB)
    background: Mapped[str | None] = mapped_column(String(2000))
    current_revision: Mapped[int] = mapped_column(Integer)
    source_document_id: Mapped[UUID | None] = mapped_column(Uuid)
    analysis_id: Mapped[UUID | None] = mapped_column(Uuid)
    created_by_subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class CharacterRevision(Base):
    """Append-only character edit history."""

    __tablename__ = "story_bible_character_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id", "character_id"],
            [
                "story_bible_characters.tenant_id",
                "story_bible_characters.project_id",
                "story_bible_characters.story_bible_id",
                "story_bible_characters.id",
            ],
            name="fk_character_revisions_character",
        ),
        UniqueConstraint(
            "tenant_id", "project_id", "story_bible_id", "character_id", "revision",
            name="uq_character_revisions_scope_revision",
        ),
        CheckConstraint("revision >= 0", name="ck_character_revisions_revision"),
        CheckConstraint("length(btrim(subject)) > 0", name="ck_character_revisions_subject"),
        CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name="ck_character_revisions_json",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    story_bible_id: Mapped[UUID] = mapped_column(Uuid)
    character_id: Mapped[UUID] = mapped_column(Uuid)
    revision: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WorldEntry(Base):
    """A world-building element: location, organization, rule, item, or other."""

    __tablename__ = "story_bible_world_entries"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id"],
            ["story_bibles.tenant_id", "story_bibles.project_id", "story_bibles.id"],
            name="fk_world_entries_story_bible",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_world_entries_project",
        ),
        UniqueConstraint(
            "tenant_id", "project_id", "story_bible_id", "id",
            name="uq_world_entries_scope_id",
        ),
        CheckConstraint(
            "entry_type IN ('location', 'organization', 'rule', 'item', 'other')",
            name="ck_world_entries_type",
        ),
        CheckConstraint(
            "length(btrim(name)) > 0 AND length(btrim(created_by_subject)) > 0",
            name="ck_world_entries_text",
        ),
        CheckConstraint(
            "jsonb_typeof(aliases) = 'array' AND jsonb_array_length(aliases) <= 10",
            name="ck_world_entries_json",
        ),
        CheckConstraint(
            "current_revision >= 0",
            name="ck_world_entries_revision",
        ),
        Index(
            "ix_world_entries_bible", "tenant_id", "project_id", "story_bible_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    story_bible_id: Mapped[UUID] = mapped_column(Uuid)
    entry_type: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(200))
    aliases: Mapped[list[str]] = mapped_column(JSONB)
    description: Mapped[str] = mapped_column(String(2000))
    current_revision: Mapped[int] = mapped_column(Integer)
    created_by_subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class WorldEntryRevision(Base):
    """Append-only world entry edit history."""

    __tablename__ = "story_bible_world_entry_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id", "entry_id"],
            [
                "story_bible_world_entries.tenant_id",
                "story_bible_world_entries.project_id",
                "story_bible_world_entries.story_bible_id",
                "story_bible_world_entries.id",
            ],
            name="fk_world_entry_revisions_entry",
        ),
        UniqueConstraint(
            "tenant_id", "project_id", "story_bible_id", "entry_id", "revision",
            name="uq_world_entry_revisions_scope_revision",
        ),
        CheckConstraint("revision >= 0", name="ck_world_entry_revisions_revision"),
        CheckConstraint("length(btrim(subject)) > 0", name="ck_world_entry_revisions_subject"),
        CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name="ck_world_entry_revisions_json",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    story_bible_id: Mapped[UUID] = mapped_column(Uuid)
    entry_id: Mapped[UUID] = mapped_column(Uuid)
    revision: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class Foreshadowing(Base):
    """A planted narrative foreshadowing that may be resolved later."""

    __tablename__ = "story_bible_foreshadowing"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id"],
            ["story_bibles.tenant_id", "story_bibles.project_id", "story_bibles.id"],
            name="fk_foreshadowing_story_bible",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_foreshadowing_project",
        ),
        UniqueConstraint(
            "tenant_id", "project_id", "story_bible_id", "id",
            name="uq_foreshadowing_scope_id",
        ),
        CheckConstraint(
            "status IN ('planted', 'resolved')",
            name="ck_foreshadowing_status",
        ),
        CheckConstraint(
            "length(btrim(title)) > 0 AND length(btrim(description)) > 0 "
            "AND length(btrim(created_by_subject)) > 0",
            name="ck_foreshadowing_text",
        ),
        CheckConstraint(
            "(status = 'resolved') = (resolution IS NOT NULL AND resolved_at IS NOT NULL)",
            name="ck_foreshadowing_resolved",
        ),
        CheckConstraint(
            "current_revision >= 0",
            name="ck_foreshadowing_revision",
        ),
        Index(
            "ix_foreshadowing_bible", "tenant_id", "project_id", "story_bible_id", "status",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    story_bible_id: Mapped[UUID] = mapped_column(Uuid)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(2000))
    status: Mapped[str] = mapped_column(String(20))
    resolution: Mapped[str | None] = mapped_column(String(2000))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    current_revision: Mapped[int] = mapped_column(Integer)
    created_by_subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ForeshadowingRevision(Base):
    """Append-only foreshadowing edit history."""

    __tablename__ = "story_bible_foreshadowing_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id", "foreshadowing_id"],
            [
                "story_bible_foreshadowing.tenant_id",
                "story_bible_foreshadowing.project_id",
                "story_bible_foreshadowing.story_bible_id",
                "story_bible_foreshadowing.id",
            ],
            name="fk_foreshadowing_revisions_entry",
        ),
        UniqueConstraint(
            "tenant_id", "project_id", "story_bible_id", "foreshadowing_id", "revision",
            name="uq_foreshadowing_revisions_scope_revision",
        ),
        CheckConstraint("revision >= 0", name="ck_foreshadowing_revisions_revision"),
        CheckConstraint("length(btrim(subject)) > 0", name="ck_foreshadowing_revisions_subject"),
        CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name="ck_foreshadowing_revisions_json",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    story_bible_id: Mapped[UUID] = mapped_column(Uuid)
    foreshadowing_id: Mapped[UUID] = mapped_column(Uuid)
    revision: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
