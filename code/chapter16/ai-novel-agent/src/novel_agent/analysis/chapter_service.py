"""ChapterService: aggregate outline + previous chapters + story settings → LLM → persist chapter.

Phase 5 — 单章生成服务：
- 上下文 = 大纲 + 前章摘要 + 故事设定（角色/世界观/伏笔/风格/事件/关系）
- provider.generate → ChapterResult 解析
- story_chapters（1 per outline chapter）+ append-only revision
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novel_agent.analysis.chapter import ChapterResult
from novel_agent.analysis.chapter_provider import ChapterProvider
from novel_agent.analysis.contracts import AnalysisError, StrictModel
from novel_agent.analysis.outline_service import OutlineService
from novel_agent.analysis.rag_retriever import RagRetriever
from novel_agent.analysis.source_characters import collect_source_characters
from novel_agent.analysis.story_bible import StoryBibleService
from novel_agent.analysis.story_graph import StoryGraphService
from novel_agent.db.models.chapter import StoryChapter, StoryChapterRevision
from novel_agent.db.models.memory import StoryMemory
from novel_agent.db.models.project import Project
from novel_agent.vector.schema import TenantProjectScope

MAX_CHAPTER_INDEX = 500
MAX_CONTENT_LENGTH = 200_000


class ChapterPatch(StrictModel):
    """Partial patch for a generated chapter with optimistic lock."""

    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    title: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    content: Annotated[str, Field(min_length=50, max_length=MAX_CONTENT_LENGTH)] | None = None
    summary: Annotated[str, Field(min_length=1, max_length=2000)] | None = None

    @model_validator(mode="after")
    def reject_explicit_null(self) -> ChapterPatch:
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("omit unchanged fields instead of null")
        return self


class ChapterService:
    """Generate and manage chapter content of a project's outline."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        story_bible: StoryBibleService,
        story_graph: StoryGraphService,
        outline: OutlineService,
        provider: ChapterProvider,
        retriever: RagRetriever | None = None,
    ) -> None:
        self._sessions = session_factory
        self._story_bible = story_bible
        self._story_graph = story_graph
        self._outline = outline
        self._provider = provider
        self._retriever = retriever

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

    async def _chapter(
        self,
        session: AsyncSession,
        scope: TenantProjectScope,
        outline_id: UUID,
        chapter_index: int,
        *,
        write: bool,
    ) -> StoryChapter:
        result = await session.execute(
            select(StoryChapter)
            .where(
                StoryChapter.tenant_id == scope.tenant_id,
                StoryChapter.project_id == scope.project_id,
                StoryChapter.outline_id == outline_id,
                StoryChapter.chapter_index == chapter_index,
            )
            .with_for_update(read=not write)
        )
        chapter = result.scalar_one_or_none()
        if chapter is None:
            raise AnalysisError("chapter_not_found", 404)
        return chapter

    @staticmethod
    def _snapshot(chapter: StoryChapter) -> dict[str, Any]:
        return {
            "title": chapter.title,
            "content": chapter.content,
            "summary": chapter.summary,
        }

    @staticmethod
    def _dict(chapter: StoryChapter) -> dict[str, Any]:
        return {
            "id": str(chapter.id),
            "outline_id": str(chapter.outline_id),
            "chapter_index": chapter.chapter_index,
            "title": chapter.title,
            "content": chapter.content,
            "summary": chapter.summary,
            "current_revision": chapter.current_revision,
            "created_by_subject": chapter.created_by_subject,
            "created_at": chapter.created_at.isoformat(),
            "updated_at": chapter.updated_at.isoformat(),
        }

    async def _aggregate_context(
        self,
        scope: TenantProjectScope,
        outline: dict[str, Any],
        outline_chapters: list[dict[str, Any]],
        chapter_index: int,
        previous_rows: list[StoryChapter],
    ) -> dict[str, Any]:
        """聚合单章生成上下文：大纲 + 当前章 + 前章摘要 + 故事设定。"""
        current = next(
            (c for c in outline_chapters if c["order_index"] == chapter_index),
            None,
        )
        if current is None:
            raise AnalysisError("chapter_out_of_range", 400)

        bible = await self._story_bible.get_bible(scope)
        style = await self._story_bible.get_style_profile(scope)
        events = await self._story_graph.list_events(scope)
        relations = await self._story_graph.list_relations(scope)

        # RAG 检索范文片段（可选依赖，未配置时跳过）
        retrieved_passages: list[dict[str, Any]] = []
        if self._retriever is not None:
            rag_query = " ".join(
                filter(
                    None,
                    [
                        current["title"],
                        current["summary"],
                        " ".join(current["key_events"]),
                    ],
                )
            )
            retrieved_passages = await self._retriever.retrieve(scope, rag_query)

        # 风格画像：只保留风格属性，剔除 id/revision/时间戳等元数据
        style_fields = (
            "narrative_voice",
            "tense",
            "pacing",
            "tone",
            "vocabulary_level",
            "sentence_structure",
            "notes",
        )
        style_context = (
            {k: style.get(k) for k in style_fields if style.get(k) is not None} if style else {}
        )

        # 注入 active 人物记忆（章节扩写时保持前后设定一致）
        memories: list[dict[str, Any]] = []
        source_characters: list[dict[str, Any]] = []
        async with self._sessions() as session, session.begin():
            mem_result = await session.execute(
                select(StoryMemory)
                .where(
                    StoryMemory.tenant_id == scope.tenant_id,
                    StoryMemory.project_id == scope.project_id,
                    StoryMemory.outline_id == UUID(outline["id"]),
                    StoryMemory.status == "active",
                )
                .order_by(StoryMemory.created_at, StoryMemory.id)
            )
            memories = [
                {"category": m.category, "content": m.content} for m in mem_result.scalars().all()
            ]
            # 注入范文分析总结出的人物（特色/能力/事迹），不依赖 promote
            source_characters = await collect_source_characters(session, scope)

        return {
            "outline": {
                "title": outline.get("title"),
                "premise": outline.get("premise"),
            },
            "current_chapter": {
                "order_index": current["order_index"],
                "title": current["title"],
                "summary": current["summary"],
                "key_events": current["key_events"],
                "notes": current["notes"],
            },
            "previous_chapters": [
                {
                    "order_index": r.chapter_index,
                    "title": r.title,
                    "summary": r.summary,
                }
                for r in previous_rows
            ],
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
            "style": style_context,
            "events": events.get("items", []),
            "relations": relations.get("items", []),
            "retrieved_passages": retrieved_passages,
            "memories": memories,
            "source_characters": source_characters,
        }

    # -- public API --

    async def generate_chapter(
        self,
        scope: TenantProjectScope,
        subject: str,
        chapter_index: int,
    ) -> dict[str, Any]:
        """聚合上下文 → LLM → 创建或更新单章正文。"""
        if chapter_index < 1 or chapter_index > MAX_CHAPTER_INDEX:
            raise AnalysisError("chapter_out_of_range", 400)

        outline = await self._outline.get_outline(scope)
        if outline is None:
            raise AnalysisError("outline_not_found", 404)
        outline_chapters = (await self._outline.list_chapters(scope)).get("items", [])

        # 读取前章摘要（所有小于目标章的行）
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=False)
            prev_result = await session.execute(
                select(StoryChapter)
                .where(
                    StoryChapter.tenant_id == scope.tenant_id,
                    StoryChapter.project_id == scope.project_id,
                    StoryChapter.outline_id == outline["id"],
                    StoryChapter.chapter_index < chapter_index,
                )
                .order_by(StoryChapter.chapter_index)
            )
            previous_rows = list(prev_result.scalars().all())

        context = await self._aggregate_context(
            scope,
            outline,
            outline_chapters,
            chapter_index,
            previous_rows,
        )
        try:
            raw = await self._provider.generate(context)
        except AnalysisError:
            raise
        try:
            result = ChapterResult.model_validate_json(raw)
        except Exception:
            raise AnalysisError("chapter_invalid_output", 502) from None

        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=True)
            now = datetime.now(UTC)
            existing = await session.execute(
                select(StoryChapter)
                .where(
                    StoryChapter.tenant_id == scope.tenant_id,
                    StoryChapter.project_id == scope.project_id,
                    StoryChapter.outline_id == outline["id"],
                    StoryChapter.chapter_index == chapter_index,
                )
                .with_for_update()
            )
            chapter = existing.scalar_one_or_none()
            if chapter is None:
                chapter = StoryChapter(
                    id=uuid4(),
                    tenant_id=scope.tenant_id,
                    project_id=scope.project_id,
                    outline_id=outline["id"],
                    chapter_index=chapter_index,
                    title=result.title,
                    content=result.content,
                    summary=result.summary,
                    current_revision=0,
                    created_by_subject=subject,
                    created_at=now,
                    updated_at=now,
                )
                session.add(chapter)
                await session.flush()
            else:
                chapter.title = result.title
                chapter.content = result.content
                chapter.summary = result.summary
                chapter.current_revision += 1
                chapter.updated_at = now
                await session.flush()

            snapshot = self._snapshot(chapter)
            revision = StoryChapterRevision(
                id=uuid4(),
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                outline_id=outline["id"],
                chapter_id=chapter.id,
                revision=chapter.current_revision,
                snapshot=deepcopy(snapshot),
                subject=subject,
                created_at=now,
            )
            session.add(revision)
            await session.flush()
            return self._dict(chapter)

    async def get_chapter(
        self,
        scope: TenantProjectScope,
        chapter_index: int,
    ) -> dict[str, Any] | None:
        outline = await self._outline.get_outline(scope)
        if outline is None:
            raise AnalysisError("outline_not_found", 404)
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=False)
            result = await session.execute(
                select(StoryChapter).where(
                    StoryChapter.tenant_id == scope.tenant_id,
                    StoryChapter.project_id == scope.project_id,
                    StoryChapter.outline_id == outline["id"],
                    StoryChapter.chapter_index == chapter_index,
                )
            )
            chapter = result.scalar_one_or_none()
            if chapter is None:
                return None
            return self._dict(chapter)

    async def list_chapters(self, scope: TenantProjectScope) -> dict[str, Any]:
        outline = await self._outline.get_outline(scope)
        if outline is None:
            raise AnalysisError("outline_not_found", 404)
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=False)
            result = await session.execute(
                select(StoryChapter)
                .where(
                    StoryChapter.tenant_id == scope.tenant_id,
                    StoryChapter.project_id == scope.project_id,
                    StoryChapter.outline_id == outline["id"],
                )
                .order_by(StoryChapter.chapter_index)
            )
            chapters = list(result.scalars().all())
            return {"items": [self._dict(c) for c in chapters]}

    async def patch_chapter(
        self,
        scope: TenantProjectScope,
        chapter_index: int,
        patch: ChapterPatch,
        subject: str,
    ) -> dict[str, Any]:
        outline = await self._outline.get_outline(scope)
        if outline is None:
            raise AnalysisError("outline_not_found", 404)
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=True)
            chapter = await self._chapter(
                session,
                scope,
                outline["id"],
                chapter_index,
                write=True,
            )
            if chapter.current_revision != patch.expected_revision:
                raise AnalysisError("chapter_revision_conflict", 409)
            edits = patch.model_dump(exclude_unset=True, exclude={"expected_revision"})
            now = datetime.now(UTC)
            for field_name, value in edits.items():
                setattr(chapter, field_name, value)
            chapter.current_revision += 1
            chapter.updated_at = now
            await session.flush()
            snapshot = self._snapshot(chapter)
            revision = StoryChapterRevision(
                id=uuid4(),
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                outline_id=outline["id"],
                chapter_id=chapter.id,
                revision=chapter.current_revision,
                snapshot=deepcopy(snapshot),
                subject=subject,
                created_at=now,
            )
            session.add(revision)
            await session.flush()
            return self._dict(chapter)

    async def aclose(self) -> None:
        await self._provider.aclose()
