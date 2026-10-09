"""Memory ORM: writing-memory items extracted from chapters with append-only revisions.

Phase 6 — 写作记忆持久化层，与 story_chapters 模式一致：
- 复合外键 (tenant_id, project_id, outline_id) → story_outlines
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


class StoryMemory(Base):
    """A single extracted writing-memory fact tied to its source chapter."""

    __tablename__ = "story_memories"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "outline_id"],
            [
                "story_outlines.tenant_id",
                "story_outlines.project_id",
                "story_outlines.id",
            ],
            name="fk_memories_outline",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "outline_id",
            "id",
            name="uq_memories_scope_id",
        ),
        CheckConstraint(
            "chapter_index IS NULL OR (chapter_index >= 1 AND chapter_index <= 500)",
            name="ck_memories_order",
        ),
        CheckConstraint(
            "current_revision >= 0",
            name="ck_memories_revision",
        ),
        CheckConstraint(
            "category IN ('character', 'world', 'plot', 'foreshadowing', 'relation')",
            name="ck_memories_category",
        ),
        CheckConstraint(
            "status IN ('active', 'superseded')",
            name="ck_memories_status",
        ),
        CheckConstraint(
            "length(btrim(content)) > 0",
            name="ck_memories_content",
        ),
        CheckConstraint(
            "length(btrim(created_by_subject)) > 0",
            name="ck_memories_subject",
        ),
        Index(
            "ix_memories_outline",
            "tenant_id",
            "project_id",
            "outline_id",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    outline_id: Mapped[UUID] = mapped_column(Uuid)
    chapter_index: Mapped[int | None] = mapped_column(Integer)
    source_document_id: Mapped[UUID | None] = mapped_column(Uuid)
    category: Mapped[str] = mapped_column(String(30))
    content: Mapped[str] = mapped_column(String(2000))
    status: Mapped[str] = mapped_column(String(20))
    current_revision: Mapped[int] = mapped_column(Integer)
    created_by_subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class StoryMemoryRevision(Base):
    """Append-only memory edit history."""

    __tablename__ = "story_memory_revisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id", "outline_id", "memory_id"],
            [
                "story_memories.tenant_id",
                "story_memories.project_id",
                "story_memories.outline_id",
                "story_memories.id",
            ],
            name="fk_memory_revisions_memory",
            ondelete="CASCADE",
        ),
        UniqueConstraint(
            "tenant_id",
            "project_id",
            "outline_id",
            "memory_id",
            "revision",
            name="uq_memory_revisions_scope_revision",
        ),
        CheckConstraint("revision >= 0", name="ck_memory_revisions_revision"),
        CheckConstraint("length(btrim(subject)) > 0", name="ck_memory_revisions_subject"),
        CheckConstraint(
            "jsonb_typeof(snapshot) = 'object'",
            name="ck_memory_revisions_json",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    tenant_id: Mapped[UUID] = mapped_column(Uuid)
    project_id: Mapped[UUID] = mapped_column(Uuid)
    outline_id: Mapped[UUID] = mapped_column(Uuid)
    memory_id: Mapped[UUID] = mapped_column(Uuid)
    revision: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB)
    subject: Mapped[str] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
