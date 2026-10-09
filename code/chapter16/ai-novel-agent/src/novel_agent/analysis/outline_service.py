"""OutlineService: aggregate story bible + graph context → LLM → persist outline/chapters.

Phase 4 — 大纲生成服务，模式与 story_bible/story_graph/extraction_service 一致：
- 上下文聚合：characters/world_entries/foreshadowing/style/events/relations
- provider.generate → OutlineResult 解析
- story_outlines（1:1 per project）+ story_outline_chapters（N per outline）
- 主表 + append-only revision
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import Field, model_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novel_agent.analysis.contracts import AnalysisError, StrictModel
from novel_agent.analysis.outline import OutlineResult
from novel_agent.analysis.outline_provider import OutlineProvider
from novel_agent.analysis.rag_retriever import RagRetriever
from novel_agent.analysis.source_characters import collect_source_characters
from novel_agent.analysis.story_bible import StoryBibleService
from novel_agent.analysis.story_graph import StoryGraphService
from novel_agent.db.models.memory import StoryMemory
from novel_agent.db.models.outline import (
    StoryOutline,
    StoryOutlineChapter,
    StoryOutlineChapterRevision,
    StoryOutlineRevision,
)
from novel_agent.db.models.project import Project
from novel_agent.db.models.story_bible import Foreshadowing, WorldEntry
from novel_agent.vector.schema import TenantProjectScope

MAX_TARGET_CHAPTERS = 500
MAX_KEY_EVENTS = 20
MAX_EVENT_TEXT = 500
MAX_NOTES = 2000


class OutlinePatch(StrictModel):
    """Partial patch for outline metadata with optimistic lock."""

    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    title: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    premise: Annotated[str, Field(min_length=1, max_length=2000)] | None = None
    target_chapters: Annotated[int, Field(strict=True, ge=1, le=MAX_TARGET_CHAPTERS)] | None = None

    @model_validator(mode="after")
    def reject_explicit_null(self) -> OutlinePatch:
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("omit unchanged fields instead of null")
        return self


class ChapterPatch(StrictModel):
    """Partial patch for a single outline chapter with optimistic lock."""

    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    title: Annotated[str, Field(min_length=1, max_length=200)] | None = None
    summary: Annotated[str, Field(min_length=1, max_length=2000)] | None = None
    key_events: (
        Annotated[
            list[Annotated[str, Field(min_length=1, max_length=MAX_EVENT_TEXT)]],
            Field(max_length=MAX_KEY_EVENTS),
        ]
        | None
    ) = None
    notes: Annotated[str, Field(max_length=MAX_NOTES)] | None = None

    @model_validator(mode="after")
    def reject_explicit_null(self) -> ChapterPatch:
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("omit unchanged fields instead of null")
        return self


class OutlineService:
    """Generate and manage the novel outline of a project."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        story_bible: StoryBibleService,
        story_graph: StoryGraphService,
        provider: OutlineProvider,
        retriever: RagRetriever | None = None,
    ) -> None:
        self._sessions = session_factory
        self._story_bible = story_bible
        self._story_graph = story_graph
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

    async def _outline(
        self, session: AsyncSession, scope: TenantProjectScope, *, write: bool
    ) -> StoryOutline:
        result = await session.execute(
            select(StoryOutline)
            .where(
                StoryOutline.tenant_id == scope.tenant_id,
                StoryOutline.project_id == scope.project_id,
            )
            .with_for_update(read=not write)
        )
        outline = result.scalar_one_or_none()
        if outline is None:
            raise AnalysisError("outline_not_found", 404)
        return outline

    async def _chapter(
        self,
        session: AsyncSession,
        scope: TenantProjectScope,
        outline: StoryOutline,
        chapter_id: UUID,
        *,
        write: bool,
    ) -> StoryOutlineChapter:
        result = await session.execute(
            select(StoryOutlineChapter)
            .where(
                StoryOutlineChapter.tenant_id == scope.tenant_id,
                StoryOutlineChapter.project_id == scope.project_id,
                StoryOutlineChapter.outline_id == outline.id,
                StoryOutlineChapter.id == chapter_id,
            )
            .with_for_update(read=not write)
        )
        chapter = result.scalar_one_or_none()
        if chapter is None:
            raise AnalysisError("chapter_not_found", 404)
        return chapter

    @staticmethod
    def _outline_snapshot(outline: StoryOutline) -> dict[str, Any]:
        return {
            "title": outline.title,
            "premise": outline.premise,
            "target_chapters": outline.target_chapters,
        }

    @staticmethod
    def _outline_dict(outline: StoryOutline) -> dict[str, Any]:
        return {
            "id": str(outline.id),
            "tenant_id": str(outline.tenant_id),
            "project_id": str(outline.project_id),
            "title": outline.title,
            "premise": outline.premise,
            "target_chapters": outline.target_chapters,
            "current_revision": outline.current_revision,
            "created_by_subject": outline.created_by_subject,
            "created_at": outline.created_at.isoformat(),
            "updated_at": outline.updated_at.isoformat(),
        }

    @staticmethod
    def _chapter_snapshot(chapter: StoryOutlineChapter) -> dict[str, Any]:
        return {
            "order_index": chapter.order_index,
            "title": chapter.title,
            "summary": chapter.summary,
            "key_events": deepcopy(chapter.key_events),
            "notes": chapter.notes,
        }

    @staticmethod
    def _chapter_dict(chapter: StoryOutlineChapter) -> dict[str, Any]:
        return {
            "id": str(chapter.id),
            "outline_id": str(chapter.outline_id),
            "order_index": chapter.order_index,
            "title": chapter.title,
            "summary": chapter.summary,
            "key_events": list(chapter.key_events),
            "notes": chapter.notes,
            "current_revision": chapter.current_revision,
            "created_at": chapter.created_at.isoformat(),
            "updated_at": chapter.updated_at.isoformat(),
        }

    async def _aggregate_context(
        self,
        scope: TenantProjectScope,
        target_chapters: int,
        user_prompt: str | None = None,
    ) -> dict[str, Any]:
        """聚合 story_bible + story_graph 上下文用于大纲生成 prompt。"""
        bible = await self._story_bible.get_bible(scope)
        style = await self._story_bible.get_style_profile(scope)
        events = await self._story_graph.list_events(scope)
        relations = await self._story_graph.list_relations(scope)

        # 风格画像：只保留风格属性，剔除 id/revision/时间戳等元数据
        _style_fields = (
            "narrative_voice",
            "tense",
            "pacing",
            "tone",
            "vocabulary_level",
            "sentence_structure",
            "notes",
        )
        style_context = (
            {k: style.get(k) for k in _style_fields if style.get(k) is not None} if style else {}
        )

        context: dict[str, Any] = {
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
            "target_chapters": target_chapters,
        }

        # world_entries / foreshadowing 直接查询（story_bible 未暴露 list 方法）
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=False)
            world_result = await session.execute(
                select(WorldEntry)
                .where(
                    WorldEntry.tenant_id == scope.tenant_id,
                    WorldEntry.project_id == scope.project_id,
                )
                .order_by(WorldEntry.created_at, WorldEntry.id)
            )
            for entry in world_result.scalars().all():
                context["world_entries"].append(
                    {
                        "name": entry.name,
                        "entry_type": entry.entry_type,
                        "description": entry.description,
                    }
                )
            foreshadow_result = await session.execute(
                select(Foreshadowing)
                .where(
                    Foreshadowing.tenant_id == scope.tenant_id,
                    Foreshadowing.project_id == scope.project_id,
                )
                .order_by(Foreshadowing.created_at, Foreshadowing.id)
            )
            for item in foreshadow_result.scalars().all():
                context["foreshadowing"].append(
                    {
                        "title": item.title,
                        "description": item.description,
                        "status": item.status,
                    }
                )

            # 注入 active 人物记忆（项目级 1:1 outline，首次生成时为空属正常）
            memory_result = await session.execute(
                select(StoryMemory)
                .where(
                    StoryMemory.tenant_id == scope.tenant_id,
                    StoryMemory.project_id == scope.project_id,
                    StoryMemory.status == "active",
                )
                .order_by(StoryMemory.created_at, StoryMemory.id)
            )
            context["memories"] = [
                {"category": m.category, "content": m.content}
                for m in memory_result.scalars().all()
            ]

            # 注入范文分析总结出的人物（特色/能力/事迹），不依赖 promote
            context["source_characters"] = await collect_source_characters(session, scope)

        # RAG 检索范文片段（可选依赖，未配置时跳过；优先用用户创作指令检索）
        if self._retriever is not None:
            rag_query = ""
            if user_prompt and user_prompt.strip():
                rag_query = user_prompt.strip()
            else:
                rag_query_parts: list[str] = []
                for c in context["characters"]:
                    rag_query_parts.append(str(c.get("name", "")))
                for w in context["world_entries"]:
                    rag_query_parts.append(str(w.get("name", "")))
                rag_query = " ".join(filter(None, rag_query_parts))
            if rag_query.strip():
                context["retrieved_passages"] = await self._retriever.retrieve(scope, rag_query)
            else:
                context["retrieved_passages"] = []

        # 用户创作指令注入（自由创作入口）
        if user_prompt and user_prompt.strip():
            context["user_prompt"] = user_prompt.strip()

        return context

    # -- public API: generate --

    async def generate_outline(
        self,
        scope: TenantProjectScope,
        subject: str,
        target_chapters: int = 10,
        user_prompt: str | None = None,
    ) -> dict[str, Any]:
        """聚合上下文 → LLM → 创建或重建 outline + chapters。

        user_prompt 为用户自由创作指令（如"写一个赛博朋克侦探寻找失踪 AI 的故事"），
        注入上下文驱动大纲生成，同时作为 RAG 检索查询。
        """
        context = await self._aggregate_context(scope, target_chapters, user_prompt)
        try:
            raw = await self._provider.generate(context)
        except AnalysisError:
            raise
        try:
            result = OutlineResult.model_validate_json(raw)
        except Exception:
            raise AnalysisError("outline_invalid_output", 502) from None

        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=True)
            now = datetime.now(UTC)
            existing = await session.execute(
                select(StoryOutline)
                .where(
                    StoryOutline.tenant_id == scope.tenant_id,
                    StoryOutline.project_id == scope.project_id,
                )
                .with_for_update()
            )
            outline = existing.scalar_one_or_none()
            if outline is None:
                outline = StoryOutline(
                    id=uuid4(),
                    tenant_id=scope.tenant_id,
                    project_id=scope.project_id,
                    title=result.title,
                    premise=result.premise,
                    target_chapters=target_chapters,
                    current_revision=0,
                    created_by_subject=subject,
                    created_at=now,
                    updated_at=now,
                )
                session.add(outline)
                await session.flush()
            else:
                outline.title = result.title
                outline.premise = result.premise
                outline.target_chapters = target_chapters
                outline.current_revision += 1
                outline.updated_at = now
                await session.execute(
                    delete(StoryOutlineChapter).where(
                        StoryOutlineChapter.tenant_id == scope.tenant_id,
                        StoryOutlineChapter.project_id == scope.project_id,
                        StoryOutlineChapter.outline_id == outline.id,
                    )
                )
                await session.flush()

            chapters: list[StoryOutlineChapter] = []
            for ch in result.chapters:
                chapter = StoryOutlineChapter(
                    id=uuid4(),
                    tenant_id=scope.tenant_id,
                    project_id=scope.project_id,
                    outline_id=outline.id,
                    order_index=ch.order_index,
                    title=ch.title,
                    summary=ch.summary,
                    key_events=list(ch.key_events),
                    notes=ch.notes,
                    current_revision=0,
                    created_at=now,
                    updated_at=now,
                )
                session.add(chapter)
                chapters.append(chapter)
            await session.flush()

            # outline revision
            snapshot = self._outline_snapshot(outline)
            revision = StoryOutlineRevision(
                id=uuid4(),
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                outline_id=outline.id,
                revision=outline.current_revision,
                snapshot=deepcopy(snapshot),
                subject=subject,
                created_at=now,
            )
            session.add(revision)
            # chapter revisions (initial revision=0)
            for chapter in chapters:
                ch_snapshot = self._chapter_snapshot(chapter)
                ch_revision = StoryOutlineChapterRevision(
                    id=uuid4(),
                    tenant_id=scope.tenant_id,
                    project_id=scope.project_id,
                    outline_id=outline.id,
                    chapter_id=chapter.id,
                    revision=0,
                    snapshot=deepcopy(ch_snapshot),
                    subject=subject,
                    created_at=now,
                )
                session.add(ch_revision)
            await session.flush()

            return {
                "outline": self._outline_dict(outline),
                "chapters": [self._chapter_dict(c) for c in chapters],
            }

    # -- public API: read/patch outline --

    async def get_outline(
        self,
        scope: TenantProjectScope,
    ) -> dict[str, Any] | None:
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=False)
            result = await session.execute(
                select(StoryOutline).where(
                    StoryOutline.tenant_id == scope.tenant_id,
                    StoryOutline.project_id == scope.project_id,
                )
            )
            outline = result.scalar_one_or_none()
            if outline is None:
                return None
            return self._outline_dict(outline)

    async def patch_outline(
        self,
        scope: TenantProjectScope,
        patch: OutlinePatch,
        subject: str,
    ) -> dict[str, Any]:
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=True)
            outline = await self._outline(session, scope, write=True)
            if outline.current_revision != patch.expected_revision:
                raise AnalysisError("outline_revision_conflict", 409)
            edits = patch.model_dump(exclude_unset=True, exclude={"expected_revision"})
            now = datetime.now(UTC)
            for field_name, value in edits.items():
                setattr(outline, field_name, value)
            outline.current_revision += 1
            outline.updated_at = now
            await session.flush()
            snapshot = self._outline_snapshot(outline)
            revision = StoryOutlineRevision(
                id=uuid4(),
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                outline_id=outline.id,
                revision=outline.current_revision,
                snapshot=deepcopy(snapshot),
                subject=subject,
                created_at=now,
            )
            session.add(revision)
            await session.flush()
            return self._outline_dict(outline)

    # -- public API: chapters --

    async def list_chapters(self, scope: TenantProjectScope) -> dict[str, Any]:
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=False)
            outline = await self._outline(session, scope, write=False)
            result = await session.execute(
                select(StoryOutlineChapter)
                .where(
                    StoryOutlineChapter.tenant_id == scope.tenant_id,
                    StoryOutlineChapter.project_id == scope.project_id,
                    StoryOutlineChapter.outline_id == outline.id,
                )
                .order_by(StoryOutlineChapter.order_index)
            )
            chapters = list(result.scalars().all())
            return {"items": [self._chapter_dict(c) for c in chapters]}

    async def patch_chapter(
        self,
        scope: TenantProjectScope,
        chapter_id: UUID,
        patch: ChapterPatch,
        subject: str,
    ) -> dict[str, Any]:
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=True)
            outline = await self._outline(session, scope, write=True)
            chapter = await self._chapter(session, scope, outline, chapter_id, write=True)
            if chapter.current_revision != patch.expected_revision:
                raise AnalysisError("chapter_revision_conflict", 409)
            edits = patch.model_dump(exclude_unset=True, exclude={"expected_revision"})
            now = datetime.now(UTC)
            for field_name, value in edits.items():
                setattr(chapter, field_name, value)
            chapter.current_revision += 1
            chapter.updated_at = now
            await session.flush()
            snapshot = self._chapter_snapshot(chapter)
            revision = StoryOutlineChapterRevision(
                id=uuid4(),
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                outline_id=outline.id,
                chapter_id=chapter.id,
                revision=chapter.current_revision,
                snapshot=deepcopy(snapshot),
                subject=subject,
                created_at=now,
            )
            session.add(revision)
            await session.flush()
            return self._chapter_dict(chapter)

    async def aclose(self) -> None:
        await self._provider.aclose()
