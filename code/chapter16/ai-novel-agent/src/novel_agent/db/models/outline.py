"""Story Outline ORM: per-project novel outline with chapters and append-only revisions.

Phase 4 — 大纲生成持久化层，与 StyleProfile/story_bible 模式一致：
- 复合外键 (tenant_id, project_id) → projects 强制多租户隔离
- 主表 + append-only 修订表
- Guard 触发器保护不可变字段
"""

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


class StoryOutline(Base):
    """One per project (1:1). Stores the generated novel outline."""

    __tablename__ = "story_outlines"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_outlines_project",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            name="uq_outlines_scope_project",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "id",
            name="uq_outlines_scope_id",
        ),
        CheckConstraint(
            "current_revision >= 0",
            name="ck_outlines_revision",
        ),
        CheckConstraint(
            "length(btrim(created_by_subject)) > 0",
            name="ck_outlines_text",
        ),
        CheckConstraint(
            "target_chapters >= 1 AND target_chapters <= 500",
            name="ck_outlines_target_chapters",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    title: Mapped[str] = mapped_column(String(200))
    premise: Mapped[str] = mapped_column(String(2000))
    target_chapters: Mapped[int] = mapped_column(Integer)
    current_revision: Mapped[int] = mapped_column(Integer)
    created_by_subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StoryOutlineRevision(Base):
    """Append-only outline edit history."""

    __tablename__ = "story_outline_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "outline_id"],
            [
                "story_outlines.tenant_id",
                "story_outlines.project_id",
                "story_outlines.id",
            ],
            name="fk_outline_revisions_outline",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "outline_id",
            "revision",
            name="uq_outline_revisions_scope_revision",
        ),
        CheckConstraint("revision >= 0", name="ck_outline_revisions_revision"),
        CheckConstraint("length(btrim(subject)) > 0", name="ck_outline_revisions_subject"),
        CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name="ck_outline_revisions_json",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    outline_id: Mapped[UUID] = mapped_column(Uuid)
    revision: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StoryOutlineChapter(Base):
    """One chapter in the outline (N per outline)."""

    __tablename__ = "story_outline_chapters"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "outline_id"],
            [
                "story_outlines.tenant_id",
                "story_outlines.project_id",
                "story_outlines.id",
            ],
            name="fk_outline_chapters_outline",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_outline_chapters_project",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "outline_id",
            "id",
            name="uq_outline_chapters_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "outline_id",
            "order_index",
            name="uq_outline_chapters_scope_order",
        ),
        CheckConstraint(
            "order_index >= 1 AND order_index <= 500",
            name="ck_outline_chapters_order",
        ),
        CheckConstraint(
            "current_revision >= 0",
            name="ck_outline_chapters_revision",
        ),
        CheckConstraint(
            "jsonb_typeof(key_events) = 'array'",
            name="ck_outline_chapters_json",
        ),
        Index(
            "ix_outline_chapters_outline",
            "tenant_id",
            "project_id",
            "outline_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    outline_id: Mapped[UUID] = mapped_column(Uuid)
    order_index: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    summary: Mapped[str] = mapped_column(String(2000))
    key_events: Mapped[list[str]] = mapped_column(JSONB)
    notes: Mapped[str | None] = mapped_column(String(2000))
    current_revision: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StoryOutlineChapterRevision(Base):
    """Append-only chapter edit history."""

    __tablename__ = "story_outline_chapter_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
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
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "outline_id",
            "chapter_id",
            "revision",
            name="uq_outline_chapter_revisions_scope_revision",
        ),
        CheckConstraint("revision >= 0", name="ck_outline_chapter_revisions_revision"),
        CheckConstraint("length(btrim(subject)) > 0", name="ck_outline_chapter_revisions_subject"),
        CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name="ck_outline_chapter_revisions_json",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    outline_id: Mapped[UUID] = mapped_column(Uuid)
    chapter_id: Mapped[UUID] = mapped_column(Uuid)
    revision: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
