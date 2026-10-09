"""Story Graph: canonical relations, events, and event participants."""

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

MAX_EVENT_PARTICIPANTS = 100


class StoryRelation(Base):
    """A directed relation between two canonical characters in the story bible."""

    __tablename__ = "story_bible_relations"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id"],
            ["story_bibles.tenant_id", "story_bibles.project_id", "story_bibles.id"],
            name="fk_relations_story_bible",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id", "from_character_id"],
            [
                "story_bible_characters.tenant_id",
                "story_bible_characters.project_id",
                "story_bible_characters.story_bible_id",
                "story_bible_characters.id",
            ],
            name="fk_relations_from_character",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id", "to_character_id"],
            [
                "story_bible_characters.tenant_id",
                "story_bible_characters.project_id",
                "story_bible_characters.story_bible_id",
                "story_bible_characters.id",
            ],
            name="fk_relations_to_character",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_relations_project",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "story_bible_id",
            "id",
            name="uq_relations_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "story_bible_id",
            "relation_key",
            name="uq_relations_scope_key",
        ),
        CheckConstraint(
            "length(btrim(relation_type)) > 0 AND length(btrim(description)) > 0 "
            "AND length(btrim(created_by_subject)) > 0",
            name="ck_relations_text",
        ),
        CheckConstraint(
            "from_character_id <> to_character_id",
            name="ck_relations_distinct",
        ),
        CheckConstraint(
            "current_revision >= 0",
            name="ck_relations_revision",
        ),
        Index("ix_relations_bible", "tenant_id", "project_id", "story_bible_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    story_bible_id: Mapped[UUID] = mapped_column(Uuid)
    relation_key: Mapped[str] = mapped_column(String(120))
    relation_type: Mapped[str] = mapped_column(String(50))
    from_character_id: Mapped[UUID] = mapped_column(Uuid)
    to_character_id: Mapped[UUID] = mapped_column(Uuid)
    description: Mapped[str] = mapped_column(String(500))
    current_revision: Mapped[int] = mapped_column(Integer)
    created_by_subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StoryRelationRevision(Base):
    """Append-only relation edit history."""

    __tablename__ = "story_bible_relation_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id", "relation_id"],
            [
                "story_bible_relations.tenant_id",
                "story_bible_relations.project_id",
                "story_bible_relations.story_bible_id",
                "story_bible_relations.id",
            ],
            name="fk_relation_revisions_relation",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "story_bible_id",
            "relation_id",
            "revision",
            name="uq_relation_revisions_scope_revision",
        ),
        CheckConstraint("revision >= 0", name="ck_relation_revisions_revision"),
        CheckConstraint("length(btrim(subject)) > 0", name="ck_relation_revisions_subject"),
        CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name="ck_relation_revisions_json",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    story_bible_id: Mapped[UUID] = mapped_column(Uuid)
    relation_id: Mapped[UUID] = mapped_column(Uuid)
    revision: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StoryEvent(Base):
    """A narrative event that may involve one or more canonical characters."""

    __tablename__ = "story_bible_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id"],
            ["story_bibles.tenant_id", "story_bibles.project_id", "story_bibles.id"],
            name="fk_events_story_bible",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_events_project",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "story_bible_id",
            "id",
            name="uq_events_scope_id",
        ),
        CheckConstraint(
            "length(btrim(title)) > 0 AND length(btrim(description)) > 0 "
            "AND length(btrim(created_by_subject)) > 0",
            name="ck_events_text",
        ),
        CheckConstraint(
            "jsonb_typeof(participants) = 'array' AND jsonb_array_length(participants) <= 100",
            name="ck_events_participants",
        ),
        CheckConstraint(
            "current_revision >= 0",
            name="ck_events_revision",
        ),
        Index("ix_events_bible", "tenant_id", "project_id", "story_bible_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    story_bible_id: Mapped[UUID] = mapped_column(Uuid)
    title: Mapped[str] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(String(2000))
    occurred_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    location: Mapped[str | None] = mapped_column(String(500))
    participants: Mapped[list[dict[str, Any]]] = mapped_column(JSONB)
    event_type: Mapped[str | None] = mapped_column(String(50))
    outcome: Mapped[str | None] = mapped_column(String(500))
    current_revision: Mapped[int] = mapped_column(Integer)
    created_by_subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StoryEventRevision(Base):
    """Append-only event edit history."""

    __tablename__ = "story_bible_event_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id", "event_id"],
            [
                "story_bible_events.tenant_id",
                "story_bible_events.project_id",
                "story_bible_events.story_bible_id",
                "story_bible_events.id",
            ],
            name="fk_event_revisions_event",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "story_bible_id",
            "event_id",
            "revision",
            name="uq_event_revisions_scope_revision",
        ),
        CheckConstraint("revision >= 0", name="ck_event_revisions_revision"),
        CheckConstraint("length(btrim(subject)) > 0", name="ck_event_revisions_subject"),
        CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name="ck_event_revisions_json",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    story_bible_id: Mapped[UUID] = mapped_column(Uuid)
    event_id: Mapped[UUID] = mapped_column(Uuid)
    revision: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
