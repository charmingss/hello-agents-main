"""MemoryService: extract writing memory from chapters, manage memory items.

Phase 6 — 写作记忆服务：
- extract_memory: 读章节正文 + 已有记忆 + 故事设定 → LLM 提取 → 写入 story_memories
- 幂等：同章重复提取先把旧同章条目标记 superseded，再插入新条目
- 人工 PATCH 可修改 content/status，递增 revision
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novel_agent.analysis.chapter_service import ChapterService
from novel_agent.analysis.contracts import AnalysisError, StrictModel
from novel_agent.analysis.memory import MemoryResult
from novel_agent.analysis.memory_provider import MemoryProvider
from novel_agent.analysis.outline_service import OutlineService
from novel_agent.analysis.story_bible import StoryBibleService
from novel_agent.analysis.story_graph import StoryGraphService
from novel_agent.db.models.memory import StoryMemory, StoryMemoryRevision
from novel_agent.db.models.project import Project
from novel_agent.db.models.source import SourceDocument, SourceSection
from novel_agent.vector.schema import TenantProjectScope

MAX_CHAPTER_INDEX = 500

MEMORY_STATUS = Literal["active", "superseded"]


class MemoryPatch(StrictModel):
    """Partial patch for a memory item with optimistic lock."""

    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    content: Annotated[str, Field(min_length=1, max_length=2000)] | None = None
    status: MEMORY_STATUS | None = None

    @model_validator(mode="after")
    def reject_explicit_null(self) -> MemoryPatch:
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("omit unchanged fields instead of null")
        return self


class MemoryService:
    """Extract and manage writing memory of a project's outline."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        story_bible: StoryBibleService,
        story_graph: StoryGraphService,
        outline: OutlineService,
        chapters: ChapterService,
        provider: MemoryProvider,
    ) -> None:
        self._sessions = session_factory
        self._story_bible = story_bible
        self._story_graph = story_graph
        self._outline = outline
        self._chapters = chapters
        self._provider = provider

    # -- internal helpers --

    async def _project(
        self, session: AsyncSession, scope: TenantProjectScope, *, write: bool
    ) -> Project:
        result = await session.execute(
            select(Project)
            .where(
                Project.tenant_id == scope.tenant_id,
                Project.id == scope.project_id,
                Project.status == "active",
            )
            .with_for_update(read=not write, of=Project)
        )
        project = result.scalar_one_or_none()
        if project is None:
            raise AnalysisError("project_not_found", 404)
        return project

    @staticmethod
    def _snapshot(memory: StoryMemory) -> dict[str, Any]:
        return {
            "chapter_index": memory.chapter_index,
            "source_document_id": str(memory.source_document_id)
            if memory.source_document_id
            else None,
            "category": memory.category,
            "content": memory.content,
            "status": memory.status,
        }

    @staticmethod
    def _dict(memory: StoryMemory) -> dict[str, Any]:
        return {
            "id": str(memory.id),
            "outline_id": str(memory.outline_id),
            "chapter_index": memory.chapter_index,
            "source_document_id": str(memory.source_document_id)
            if memory.source_document_id
            else None,
            "category": memory.category,
            "content": memory.content,
            "status": memory.status,
            "current_revision": memory.current_revision,
            "created_by_subject": memory.created_by_subject,
            "created_at": memory.created_at.isoformat(),
            "updated_at": memory.updated_at.isoformat(),
        }

    async def _aggregate_context(
        self,
        scope: TenantProjectScope,
        chapter: dict[str, Any],
        existing: list[StoryMemory],
    ) -> dict[str, Any]:
        """聚合提取上下文：章节正文 + 角色/世界观/伏笔 + 已有记忆。"""
        bible = await self._story_bible.get_bible(scope)
        context: dict[str, Any] = {
            "chapter": {
                "order_index": chapter["chapter_index"],
                "title": chapter["title"],
                "content": chapter["content"],
            },
            "characters": [
                {
                    "name": c.get("name"),
                    "aliases": c.get("aliases", []),
                    "traits": c.get("traits", []),
                    "description": c.get("description", []),
                }
                for c in bible.get("items", [])
            ],
            "world_entries": [],
            "foreshadowing": [],
            "existing_memories": [
                {
                    "category": m.category,
                    "content": m.content,
                    "status": m.status,
                }
                for m in existing
            ],
        }
        return context

    async def _aggregate_source_context(
        self,
        scope: TenantProjectScope,
        source_id: UUID,
        sections: list[dict[str, Any]],
        existing: list[StoryMemory],
    ) -> dict[str, Any]:
        """聚合范文提取上下文：范文段落 + 角色 + 已有记忆。"""
        bible = await self._story_bible.get_bible(scope)
        context: dict[str, Any] = {
            "source_text": "\n\n".join(
                f"## {s.get('heading') or ''}\n{s['text']}" for s in sections
            ),
            "source_document_id": str(source_id),
            "characters": [
                {
                    "name": c.get("name"),
                    "aliases": c.get("aliases", []),
                    "traits": c.get("traits", []),
                    "description": c.get("description", []),
                }
                for c in bible.get("items", [])
            ],
            "existing_memories": [
                {
                    "category": m.category,
                    "content": m.content,
                    "status": m.status,
                }
                for m in existing
            ],
        }
        return context

    # -- public API --

    async def extract_memory(
        self,
        scope: TenantProjectScope,
        subject: str,
        chapter_index: int,
    ) -> dict[str, Any]:
        """读取章节正文 → LLM 提取 → 写入记忆库（同章旧条目标记 superseded）。"""
        if chapter_index < 1 or chapter_index > MAX_CHAPTER_INDEX:
            raise AnalysisError("chapter_out_of_range", 400)

        outline = await self._outline.get_outline(scope)
        if outline is None:
            raise AnalysisError("outline_not_found", 404)
        outline_chapters = (await self._outline.list_chapters(scope)).get("items", [])
        if not any(c["order_index"] == chapter_index for c in outline_chapters):
            raise AnalysisError("chapter_out_of_range", 400)

        chapter = await self._chapters.get_chapter(scope, chapter_index)
        if chapter is None:
            raise AnalysisError("chapter_not_found", 404)

        # 读已有记忆（active + superseded 都提供去重锚点）
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=False)
            mem_result = await session.execute(
                select(StoryMemory)
                .where(
                    StoryMemory.tenant_id == scope.tenant_id,
                    StoryMemory.project_id == scope.project_id,
                    StoryMemory.outline_id == outline["id"],
                )
                .order_by(StoryMemory.chapter_index, StoryMemory.id)
            )
            existing = list(mem_result.scalars().all())

        context = await self._aggregate_context(scope, chapter, existing)
        try:
            raw = await self._provider.generate(context)
        except AnalysisError:
            raise
        try:
            result = MemoryResult.model_validate_json(raw)
        except Exception:
            raise AnalysisError("memory_invalid_output", 502) from None

        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=True)
            now = datetime.now(UTC)
            # 旧同章条目标记 superseded
            stale_result = await session.execute(
                select(StoryMemory)
                .where(
                    StoryMemory.tenant_id == scope.tenant_id,
                    StoryMemory.project_id == scope.project_id,
                    StoryMemory.outline_id == outline["id"],
                    StoryMemory.chapter_index == chapter_index,
                    StoryMemory.status == "active",
                )
                .with_for_update()
            )
            superseded = 0
            for stale in stale_result.scalars().all():
                stale.status = "superseded"
                stale.updated_at = now
                superseded += 1
            # 插入新条目
            inserted = 0
            for item in result.items:
                memory = StoryMemory(
                    id=uuid4(),
                    tenant_id=scope.tenant_id,
                    project_id=scope.project_id,
                    outline_id=outline["id"],
                    chapter_index=chapter_index,
                    category=item.category,
                    content=item.content,
                    status="active",
                    current_revision=0,
                    created_by_subject=subject,
                    created_at=now,
                    updated_at=now,
                )
                session.add(memory)
                inserted += 1
                snapshot = self._snapshot(memory)
                revision = StoryMemoryRevision(
                    id=uuid4(),
                    tenant_id=scope.tenant_id,
                    project_id=scope.project_id,
                    outline_id=outline["id"],
                    memory_id=memory.id,
                    revision=0,
                    snapshot=deepcopy(snapshot),
                    subject=subject,
                    created_at=now,
                )
                session.add(revision)
            await session.flush()
            return {
                "outline_id": str(outline["id"]),
                "chapter_index": chapter_index,
                "extracted": inserted,
                "superseded": superseded,
            }

    async def extract_from_source(
        self,
        scope: TenantProjectScope,
        subject: str,
        source_id: UUID,
    ) -> dict[str, Any]:
        """从导入的范文提取记忆：读范文段落 → LLM 提取 → 写入 story_memories。

        同源重复提取先将旧同源 active 条目标记 superseded，再插入新条目。
        记忆的 chapter_index 为 NULL，source_document_id 标记来源。
        """
        outline = await self._outline.get_outline(scope)
        if outline is None:
            raise AnalysisError("outline_not_found", 404)

        # 验证源文档存在且已解析完成
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=False)
            doc_result = await session.execute(
                select(SourceDocument).where(
                    SourceDocument.tenant_id == scope.tenant_id,
                    SourceDocument.project_id == scope.project_id,
                    SourceDocument.id == source_id,
                )
            )
            doc = doc_result.scalar_one_or_none()
            if doc is None:
                raise AnalysisError("source_not_found", 404)
            if doc.status != "chunks_ready":
                raise AnalysisError("source_not_ready", 400)

            # 读取范文段落
            section_result = await session.execute(
                select(SourceSection)
                .where(
                    SourceSection.tenant_id == scope.tenant_id,
                    SourceSection.project_id == scope.project_id,
                    SourceSection.parse_run_id == doc.active_parse_run_id,
                )
                .order_by(SourceSection.section_index)
            )
            section_rows = list(section_result.scalars().all())

            if not section_rows:
                raise AnalysisError("source_not_ready", 400)

            sections = [
                {
                    "heading": s.heading,
                    "text": s.text,
                }
                for s in section_rows
            ]

            # 读取已有记忆（全部，作为去重锚点）
            mem_result = await session.execute(
                select(StoryMemory)
                .where(
                    StoryMemory.tenant_id == scope.tenant_id,
                    StoryMemory.project_id == scope.project_id,
                    StoryMemory.outline_id == outline["id"],
                )
                .order_by(StoryMemory.id)
            )
            existing = list(mem_result.scalars().all())

        context = await self._aggregate_source_context(scope, source_id, sections, existing)
        try:
            raw = await self._provider.generate(context)
        except AnalysisError:
            raise
        try:
            result = MemoryResult.model_validate_json(raw)
        except Exception:
            raise AnalysisError("memory_invalid_output", 502) from None

        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=True)
            now = datetime.now(UTC)
            # 旧同源条目标记 superseded
            stale_result = await session.execute(
                select(StoryMemory)
                .where(
                    StoryMemory.tenant_id == scope.tenant_id,
                    StoryMemory.project_id == scope.project_id,
                    StoryMemory.outline_id == outline["id"],
                    StoryMemory.source_document_id == source_id,
                    StoryMemory.status == "active",
                )
                .with_for_update()
            )
            superseded = 0
            for stale in stale_result.scalars().all():
                stale.status = "superseded"
                stale.updated_at = now
                superseded += 1
            # 插入新条目
            inserted = 0
            for item in result.items:
                memory = StoryMemory(
                    id=uuid4(),
                    tenant_id=scope.tenant_id,
                    project_id=scope.project_id,
                    outline_id=outline["id"],
                    chapter_index=None,
                    source_document_id=source_id,
                    category=item.category,
                    content=item.content,
                    status="active",
                    current_revision=0,
                    created_by_subject=subject,
                    created_at=now,
                    updated_at=now,
                )
                session.add(memory)
                inserted += 1
                snapshot = self._snapshot(memory)
                revision = StoryMemoryRevision(
                    id=uuid4(),
                    tenant_id=scope.tenant_id,
                    project_id=scope.project_id,
                    outline_id=outline["id"],
                    memory_id=memory.id,
                    revision=0,
                    snapshot=deepcopy(snapshot),
                    subject=subject,
                    created_at=now,
                )
                session.add(revision)
            await session.flush()
            return {
                "outline_id": str(outline["id"]),
                "source_document_id": str(source_id),
                "extracted": inserted,
                "superseded": superseded,
            }

    async def list_memories(
        self,
        scope: TenantProjectScope,
        status: str | None = None,
    ) -> dict[str, Any]:
        outline = await self._outline.get_outline(scope)
        if outline is None:
            raise AnalysisError("outline_not_found", 404)
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=False)
            stmt = (
                select(StoryMemory)
                .where(
                    StoryMemory.tenant_id == scope.tenant_id,
                    StoryMemory.project_id == scope.project_id,
                    StoryMemory.outline_id == outline["id"],
                )
                .order_by(StoryMemory.chapter_index, StoryMemory.id)
            )
            if status is not None:
                stmt = stmt.where(StoryMemory.status == status)
            result = await session.execute(stmt)
            memories = list(result.scalars().all())
            return {"items": [self._dict(m) for m in memories]}

    async def get_memory(
        self,
        scope: TenantProjectScope,
        memory_id: UUID,
    ) -> dict[str, Any] | None:
        outline = await self._outline.get_outline(scope)
        if outline is None:
            raise AnalysisError("outline_not_found", 404)
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=False)
            result = await session.execute(
                select(StoryMemory).where(
                    StoryMemory.tenant_id == scope.tenant_id,
                    StoryMemory.project_id == scope.project_id,
                    StoryMemory.outline_id == outline["id"],
                    StoryMemory.id == memory_id,
                )
            )
            memory = result.scalar_one_or_none()
            if memory is None:
                return None
            return self._dict(memory)

    async def patch_memory(
        self,
        scope: TenantProjectScope,
        memory_id: UUID,
        patch: MemoryPatch,
        subject: str,
    ) -> dict[str, Any]:
        outline = await self._outline.get_outline(scope)
        if outline is None:
            raise AnalysisError("outline_not_found", 404)
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=True)
            result = await session.execute(
                select(StoryMemory)
                .where(
                    StoryMemory.tenant_id == scope.tenant_id,
                    StoryMemory.project_id == scope.project_id,
                    StoryMemory.outline_id == outline["id"],
                    StoryMemory.id == memory_id,
                )
                .with_for_update()
            )
            memory = result.scalar_one_or_none()
            if memory is None:
                raise AnalysisError("memory_not_found", 404)
            if memory.current_revision != patch.expected_revision:
                raise AnalysisError("memory_revision_conflict", 409)
            edits = patch.model_dump(exclude_unset=True, exclude={"expected_revision"})
            now = datetime.now(UTC)
            for field_name, value in edits.items():
                setattr(memory, field_name, value)
            memory.current_revision += 1
            memory.updated_at = now
            await session.flush()
            snapshot = self._snapshot(memory)
            revision = StoryMemoryRevision(
                id=uuid4(),
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                outline_id=outline["id"],
                memory_id=memory.id,
                revision=memory.current_revision,
                snapshot=deepcopy(snapshot),
                subject=subject,
                created_at=now,
            )
            session.add(revision)
            await session.flush()
            return self._dict(memory)

    async def aclose(self) -> None:
        await self._provider.aclose()
