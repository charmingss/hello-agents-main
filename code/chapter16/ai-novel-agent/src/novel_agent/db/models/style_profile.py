"""StyleProfile ORM: per-project narrative style profile with append-only revisions."""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from novel_agent.db.base import Base


class StyleProfile(Base):
    """One per project (1:1 to Story Bible). Stores narrative style profile."""

    __tablename__ = "story_bible_style_profiles"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "story_bible_id"],
            ["story_bibles.tenant_id", "story_bibles.project_id", "story_bibles.id"],
            name="fk_style_profiles_bible",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_style_profiles_project",
        ),
        UniqueConstraint(
            "tenant_id", "project_id", "id",
            name="uq_style_profiles_scope_id",
        ),
        UniqueConstraint(
            "tenant_id", "project_id",
            name="uq_style_profiles_scope_project",
        ),
        CheckConstraint(
            "current_revision >= 0",
            name="ck_style_profiles_revision",
        ),
        CheckConstraint(
            "length(btrim(created_by_subject)) > 0",
            name="ck_style_profiles_text",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    story_bible_id: Mapped[UUID] = mapped_column(Uuid)
    narrative_voice: Mapped[str | None] = mapped_column(String(200))
    tense: Mapped[str | None] = mapped_column(String(50))
    pacing: Mapped[str | None] = mapped_column(String(200))
    tone: Mapped[str | None] = mapped_column(String(200))
    vocabulary_level: Mapped[str | None] = mapped_column(String(200))
    sentence_structure: Mapped[str | None] = mapped_column(String(200))
    notes: Mapped[str | None] = mapped_column(String(2000))
    current_revision: Mapped[int] = mapped_column(Integer)
    created_by_subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StyleProfileRevision(Base):
    """Append-only style profile edit history."""

    __tablename__ = "story_bible_style_profile_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "style_profile_id"],
            [
                "story_bible_style_profiles.tenant_id",
                "story_bible_style_profiles.project_id",
                "story_bible_style_profiles.id",
            ],
            name="fk_style_profile_revisions_profile",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id", "project_id", "style_profile_id", "revision",
            name="uq_style_profile_revisions_scope_revision",
        ),
        CheckConstraint("revision >= 0", name="ck_style_profile_revisions_revision"),
        CheckConstraint(
            "length(btrim(subject)) > 0", name="ck_style_profile_revisions_subject"
        ),
        CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name="ck_style_profile_revisions_json",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    style_profile_id: Mapped[UUID] = mapped_column(Uuid)
    revision: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
