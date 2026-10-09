"""Chapter ORM: per-outline generated chapter content with append-only revisions.

Phase 5 — 单章正文持久化层，与 story_outlines 模式一致：
- 复合外键 (tenant_id, project_id, outline_id) → story_outlines 强制多租户隔离
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
    Text,
    UniqueConstraint,
    Uuid,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from novel_agent.db.base import Base


class StoryChapter(Base):
    """One generated chapter per outline chapter (1:1 per outline order_index)."""

    __tablename__ = "story_chapters"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "outline_id"],
            [
                "story_outlines.tenant_id",
                "story_outlines.project_id",
                "story_outlines.id",
            ],
            name="fk_chapters_outline",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "outline_id",
            "id",
            name="uq_chapters_scope_id",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "outline_id",
            "chapter_index",
            name="uq_chapters_scope_order",
        ),
        CheckConstraint(
            "chapter_index >= 1 AND chapter_index <= 500",
            name="ck_chapters_order",
        ),
        CheckConstraint(
            "current_revision >= 0",
            name="ck_chapters_revision",
        ),
        CheckConstraint(
            "length(btrim(title)) > 0",
            name="ck_chapters_title",
        ),
        CheckConstraint(
            "length(btrim(created_by_subject)) > 0",
            name="ck_chapters_subject",
        ),
        Index(
            "ix_chapters_outline",
            "tenant_id",
            "project_id",
            "outline_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    outline_id: Mapped[UUID] = mapped_column(Uuid)
    chapter_index: Mapped[int] = mapped_column(Integer)
    title: Mapped[str] = mapped_column(String(200))
    content: Mapped[str] = mapped_column(Text)
    summary: Mapped[str] = mapped_column(String(2000))
    current_revision: Mapped[int] = mapped_column(Integer)
    created_by_subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StoryChapterRevision(Base):
    """Append-only chapter edit history."""

    __tablename__ = "story_chapter_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
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
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "outline_id",
            "chapter_id",
            "revision",
            name="uq_chapter_revisions_scope_revision",
        ),
        CheckConstraint("revision >= 0", name="ck_chapter_revisions_revision"),
        CheckConstraint("length(btrim(subject)) > 0", name="ck_chapter_revisions_subject"),
        CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name="ck_chapter_revisions_json",
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
