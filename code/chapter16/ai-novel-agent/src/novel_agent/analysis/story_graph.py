"""Story Graph service: canonical relations, events, and the Neo4j projection sync."""

from __future__ import annotations

import unicodedata
from copy import deepcopy
from datetime import UTC, datetime
from typing import Annotated, Any
from uuid import UUID, uuid4

from pydantic import Field, model_validator
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novel_agent.analysis.contracts import AnalysisError, StrictModel
from novel_agent.db.models.project import Project
from novel_agent.db.models.story_bible import Character, StoryBible
from novel_agent.db.models.story_graph import (
    StoryEvent,
    StoryEventRevision,
    StoryRelation,
    StoryRelationRevision,
)
from novel_agent.graph.authority import GraphAuthority, SqlAlchemyGraphAuthority
from novel_agent.graph.projection import GraphProjection
from novel_agent.graph.schema import AuthoritativeGraph
from novel_agent.graph.transport import GraphProjectionError
from novel_agent.vector.schema import TenantProjectScope

MAX_EVENT_PARTICIPANTS = 100
MAX_RELATION_DESC = 500
MAX_EVENT_DESC = 2000
MAX_EVENT_TITLE = 200
MAX_EVENT_LOCATION = 500
MAX_EVENT_TYPE = 50
MAX_EVENT_OUTCOME = 500
MAX_RELATION_TYPE = 50


def _normalize_key(value: str) -> str:
    """NFKC + strip + casefold for stable relation keys."""
    return unicodedata.normalize("NFKC", value).strip().casefold()


class RelationCreate(StrictModel):
    relation_type: Annotated[str, Field(min_length=1, max_length=MAX_RELATION_TYPE)]
    from_character_id: UUID
    to_character_id: UUID
    description: Annotated[str, Field(min_length=1, max_length=MAX_RELATION_DESC)]

    @model_validator(mode="after")
    def reject_explicit_null(self) -> RelationCreate:
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("omit unchanged fields instead of null")
        return self

    @model_validator(mode="after")
    def reject_self_relation(self) -> RelationCreate:
        if self.from_character_id == self.to_character_id:
            raise ValueError("a character cannot relate to itself")
        return self


class RelationPatch(StrictModel):
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    relation_type: Annotated[str, Field(min_length=1, max_length=MAX_RELATION_TYPE)] | None = None
    description: Annotated[str, Field(min_length=1, max_length=MAX_RELATION_DESC)] | None = None

    @model_validator(mode="after")
    def reject_explicit_null(self) -> RelationPatch:
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("omit unchanged fields instead of null")
        return self


class EventParticipant(StrictModel):
    character_id: UUID
    role: Annotated[str, Field(min_length=1, max_length=50)]
    order_index: Annotated[int, Field(ge=0)] = 0


class EventCreate(StrictModel):
    title: Annotated[str, Field(min_length=1, max_length=MAX_EVENT_TITLE)]
    description: Annotated[str, Field(min_length=1, max_length=MAX_EVENT_DESC)]
    occurred_at: datetime | None = None
    location: Annotated[str, Field(max_length=MAX_EVENT_LOCATION)] | None = None
    participants: Annotated[list[EventParticipant], Field(max_length=MAX_EVENT_PARTICIPANTS)] = []
    event_type: Annotated[str, Field(max_length=MAX_EVENT_TYPE)] | None = None
    outcome: Annotated[str, Field(max_length=MAX_EVENT_OUTCOME)] | None = None

    @model_validator(mode="after")
    def reject_explicit_null(self) -> EventCreate:
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("omit unchanged fields instead of null")
        return self

    @model_validator(mode="after")
    def reject_duplicate_participants(self) -> EventCreate:
        ids = [p.character_id for p in self.participants]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate participant character_id")
        return self


class EventPatch(StrictModel):
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    title: Annotated[str, Field(min_length=1, max_length=MAX_EVENT_TITLE)] | None = None
    description: Annotated[str, Field(min_length=1, max_length=MAX_EVENT_DESC)] | None = None
    occurred_at: datetime | None = None
    location: Annotated[str, Field(max_length=MAX_EVENT_LOCATION)] | None = None
    participants: (
        Annotated[list[EventParticipant], Field(max_length=MAX_EVENT_PARTICIPANTS)] | None
    ) = None
    event_type: Annotated[str, Field(max_length=MAX_EVENT_TYPE)] | None = None
    outcome: Annotated[str, Field(max_length=MAX_EVENT_OUTCOME)] | None = None

    @model_validator(mode="after")
    def reject_explicit_null(self) -> EventPatch:
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("omit unchanged fields instead of null")
        return self

    @model_validator(mode="after")
    def reject_duplicate_participants(self) -> EventPatch:
        if self.participants is None:
            return self
        ids = [p.character_id for p in self.participants]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate participant character_id")
        return self


class StoryGraphService:
    """Service for canonical story relations/events and graph projection sync."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        authority: GraphAuthority | None = None,
        projection: GraphProjection | None = None,
    ) -> None:
        self._sessions = session_factory
        self._authority = authority or SqlAlchemyGraphAuthority(session_factory)
        self._projection = projection

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

    async def _bible(
        self,
        session: AsyncSession,
        scope: TenantProjectScope,
    ) -> StoryBible:
        result = await session.execute(
            select(StoryBible).where(
                StoryBible.tenant_id == scope.tenant_id,
                StoryBible.project_id == scope.project_id,
            )
        )
        bible = result.scalar_one_or_none()
        if bible is None:
            raise AnalysisError("story_bible_not_found", 404)
        return bible

    async def _get_or_create_bible(
        self,
        session: AsyncSession,
        scope: TenantProjectScope,
        subject: str,
    ) -> StoryBible:
        result = await session.execute(
            select(StoryBible)
            .where(
                StoryBible.tenant_id == scope.tenant_id,
                StoryBible.project_id == scope.project_id,
            )
            .with_for_update()
        )
        bible = result.scalar_one_or_none()
        if bible is not None:
            return bible
        now = datetime.now(UTC)
        bible = StoryBible(
            id=uuid4(),
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            created_by_subject=subject,
            created_at=now,
            updated_at=now,
        )
        session.add(bible)
        await session.flush()
        return bible

    async def _character(
        self,
        session: AsyncSession,
        scope: TenantProjectScope,
        character_id: UUID,
    ) -> Character:
        bible = await self._bible(session, scope)
        result = await session.execute(
            select(Character)
            .where(
                Character.tenant_id == scope.tenant_id,
                Character.project_id == scope.project_id,
                Character.story_bible_id == bible.id,
                Character.id == character_id,
            )
            .with_for_update(read=True)
        )
        character = result.scalar_one_or_none()
        if character is None:
            raise AnalysisError("character_not_found", 404)
        return character

    async def _relation(
        self,
        session: AsyncSession,
        scope: TenantProjectScope,
        relation_id: UUID,
        *,
        write: bool,
    ) -> StoryRelation:
        bible = await self._bible(session, scope)
        result = await session.execute(
            select(StoryRelation)
            .where(
                StoryRelation.tenant_id == scope.tenant_id,
                StoryRelation.project_id == scope.project_id,
                StoryRelation.story_bible_id == bible.id,
                StoryRelation.id == relation_id,
            )
            .with_for_update(read=not write)
        )
        relation = result.scalar_one_or_none()
        if relation is None:
            raise AnalysisError("relation_not_found", 404)
        return relation

    async def _event(
        self,
        session: AsyncSession,
        scope: TenantProjectScope,
        event_id: UUID,
        *,
        write: bool,
    ) -> StoryEvent:
        bible = await self._bible(session, scope)
        result = await session.execute(
            select(StoryEvent)
            .where(
                StoryEvent.tenant_id == scope.tenant_id,
                StoryEvent.project_id == scope.project_id,
                StoryEvent.story_bible_id == bible.id,
                StoryEvent.id == event_id,
            )
            .with_for_update(read=not write)
        )
        event = result.scalar_one_or_none()
        if event is None:
            raise AnalysisError("event_not_found", 404)
        return event

    @staticmethod
    def _relation_snapshot(relation: StoryRelation) -> dict[str, Any]:
        return {
            "relation_type": relation.relation_type,
            "from_character_id": str(relation.from_character_id),
            "to_character_id": str(relation.to_character_id),
            "description": relation.description,
        }

    @staticmethod
    def _event_snapshot(event: StoryEvent) -> dict[str, Any]:
        return {
            "title": event.title,
            "description": event.description,
            "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
            "location": event.location,
            "participants": deepcopy(event.participants or []),
            "event_type": event.event_type,
            "outcome": event.outcome,
        }

    @staticmethod
    def _relation_dict(relation: StoryRelation) -> dict[str, Any]:
        return {
            "id": str(relation.id),
            "story_bible_id": str(relation.story_bible_id),
            "relation_type": relation.relation_type,
            "from_character_id": str(relation.from_character_id),
            "to_character_id": str(relation.to_character_id),
            "description": relation.description,
            "current_revision": relation.current_revision,
            "created_by_subject": relation.created_by_subject,
            "created_at": relation.created_at.isoformat(),
            "updated_at": relation.updated_at.isoformat(),
        }

    @staticmethod
    def _event_dict(event: StoryEvent) -> dict[str, Any]:
        return {
            "id": str(event.id),
            "story_bible_id": str(event.story_bible_id),
            "title": event.title,
            "description": event.description,
            "occurred_at": event.occurred_at.isoformat() if event.occurred_at else None,
            "location": event.location,
            "participants": [
                {
                    "character_id": str(p["character_id"]),
                    "role": p["role"],
                    "order_index": p.get("order_index", 0),
                }
                for p in event.participants or []
            ],
            "event_type": event.event_type,
            "outcome": event.outcome,
            "current_revision": event.current_revision,
            "created_by_subject": event.created_by_subject,
            "created_at": event.created_at.isoformat(),
            "updated_at": event.updated_at.isoformat(),
        }

    # -- public API: relations --

    async def list_relations(self, scope: TenantProjectScope) -> dict[str, Any]:
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=False)
            bible = await self._bible(session, scope)
            result = await session.execute(
                select(StoryRelation)
                .where(
                    StoryRelation.tenant_id == scope.tenant_id,
                    StoryRelation.project_id == scope.project_id,
                    StoryRelation.story_bible_id == bible.id,
                )
                .order_by(StoryRelation.created_at, StoryRelation.id)
            )
            relations = list(result.scalars().all())
            return {"items": [self._relation_dict(r) for r in relations]}

    async def create_relation(
        self,
        scope: TenantProjectScope,
        data: RelationCreate,
        subject: str,
    ) -> dict[str, Any]:
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=True)
            bible = await self._get_or_create_bible(session, scope, subject)
            # Validate both endpoints exist before inserting.
            await self._character(session, scope, data.from_character_id)
            await self._character(session, scope, data.to_character_id)
            key = _normalize_key(
                f"{data.relation_type}\u0000{data.from_character_id}\u0000{data.to_character_id}"
            )
            existing = await session.execute(
                select(StoryRelation).where(
                    StoryRelation.tenant_id == scope.tenant_id,
                    StoryRelation.project_id == scope.project_id,
                    StoryRelation.story_bible_id == bible.id,
                    StoryRelation.relation_key == key,
                )
            )
            if existing.scalar_one_or_none() is not None:
                raise AnalysisError("relation_already_exists", 409)
            now = datetime.now(UTC)
            relation = StoryRelation(
                id=uuid4(),
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                story_bible_id=bible.id,
                relation_key=key,
                relation_type=data.relation_type,
                from_character_id=data.from_character_id,
                to_character_id=data.to_character_id,
                description=data.description,
                current_revision=0,
                created_by_subject=subject,
                created_at=now,
                updated_at=now,
            )
            session.add(relation)
            await session.flush()
            snapshot = self._relation_snapshot(relation)
            revision = StoryRelationRevision(
                id=uuid4(),
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                story_bible_id=bible.id,
                relation_id=relation.id,
                revision=0,
                snapshot=deepcopy(snapshot),
                subject=subject,
                created_at=now,
            )
            session.add(revision)
            await session.flush()
            bible.updated_at = now
            return self._relation_dict(relation)

    async def get_relation(
        self,
        scope: TenantProjectScope,
        relation_id: UUID,
    ) -> dict[str, Any]:
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=False)
            relation = await self._relation(session, scope, relation_id, write=False)
            return self._relation_dict(relation)

    async def patch_relation(
        self,
        scope: TenantProjectScope,
        relation_id: UUID,
        patch: RelationPatch,
        subject: str,
    ) -> dict[str, Any]:
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=True)
            relation = await self._relation(session, scope, relation_id, write=True)
            if relation.current_revision != patch.expected_revision:
                raise AnalysisError("relation_revision_conflict", 409)
            edits = patch.model_dump(exclude_unset=True, exclude={"expected_revision"})
            now = datetime.now(UTC)
            if "relation_type" in edits and edits["relation_type"] != relation.relation_type:
                new_key = _normalize_key(
                    f"{edits['relation_type']}\u0000"
                    f"{relation.from_character_id}\u0000{relation.to_character_id}"
                )
                existing = await session.execute(
                    select(StoryRelation).where(
                        StoryRelation.tenant_id == scope.tenant_id,
                        StoryRelation.project_id == scope.project_id,
                        StoryRelation.story_bible_id == relation.story_bible_id,
                        StoryRelation.relation_key == new_key,
                        StoryRelation.id != relation.id,
                    )
                )
                if existing.scalar_one_or_none() is not None:
                    raise AnalysisError("relation_already_exists", 409)
                relation.relation_key = new_key
            for field_name, value in edits.items():
                setattr(relation, field_name, value)
            relation.current_revision += 1
            relation.updated_at = now
            await session.flush()
            snapshot = self._relation_snapshot(relation)
            revision = StoryRelationRevision(
                id=uuid4(),
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                story_bible_id=relation.story_bible_id,
                relation_id=relation.id,
                revision=relation.current_revision,
                snapshot=deepcopy(snapshot),
                subject=subject,
                created_at=now,
            )
            session.add(revision)
            await session.flush()
            return self._relation_dict(relation)

    # -- public API: events --

    async def list_events(self, scope: TenantProjectScope) -> dict[str, Any]:
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=False)
            bible = await self._bible(session, scope)
            result = await session.execute(
                select(StoryEvent)
                .where(
                    StoryEvent.tenant_id == scope.tenant_id,
                    StoryEvent.project_id == scope.project_id,
                    StoryEvent.story_bible_id == bible.id,
                )
                .order_by(StoryEvent.created_at, StoryEvent.id)
            )
            events = list(result.scalars().all())
            return {"items": [self._event_dict(e) for e in events]}

    async def create_event(
        self,
        scope: TenantProjectScope,
        data: EventCreate,
        subject: str,
    ) -> dict[str, Any]:
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=True)
            bible = await self._get_or_create_bible(session, scope, subject)
            for participant in data.participants:
                await self._character(session, scope, participant.character_id)
            now = datetime.now(UTC)
            event = StoryEvent(
                id=uuid4(),
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                story_bible_id=bible.id,
                title=data.title,
                description=data.description,
                occurred_at=data.occurred_at,
                location=data.location,
                event_type=data.event_type,
                outcome=data.outcome,
                participants=[
                    {
                        "character_id": str(p.character_id),
                        "role": p.role,
                        "order_index": p.order_index,
                    }
                    for p in data.participants
                ],
                current_revision=0,
                created_by_subject=subject,
                created_at=now,
                updated_at=now,
            )
            session.add(event)
            await session.flush()
            snapshot = self._event_snapshot(event)
            revision = StoryEventRevision(
                id=uuid4(),
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                story_bible_id=bible.id,
                event_id=event.id,
                revision=0,
                snapshot=deepcopy(snapshot),
                subject=subject,
                created_at=now,
            )
            session.add(revision)
            await session.flush()
            bible.updated_at = now
            return self._event_dict(event)

    async def get_event(
        self,
        scope: TenantProjectScope,
        event_id: UUID,
    ) -> dict[str, Any]:
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=False)
            event = await self._event(session, scope, event_id, write=False)
            return self._event_dict(event)

    async def patch_event(
        self,
        scope: TenantProjectScope,
        event_id: UUID,
        patch: EventPatch,
        subject: str,
    ) -> dict[str, Any]:
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=True)
            event = await self._event(session, scope, event_id, write=True)
            if event.current_revision != patch.expected_revision:
                raise AnalysisError("event_revision_conflict", 409)
            edits = patch.model_dump(exclude_unset=True, exclude={"expected_revision"})
            now = datetime.now(UTC)
            if "participants" in edits:
                for participant in edits["participants"]:
                    await self._character(session, scope, participant["character_id"])
            for field_name, value in edits.items():
                setattr(event, field_name, value)
            event.current_revision += 1
            event.updated_at = now
            await session.flush()
            snapshot = self._event_snapshot(event)
            revision = StoryEventRevision(
                id=uuid4(),
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                story_bible_id=event.story_bible_id,
                event_id=event.id,
                revision=event.current_revision,
                snapshot=deepcopy(snapshot),
                subject=subject,
                created_at=now,
            )
            session.add(revision)
            await session.flush()
            return self._event_dict(event)

    # -- public API: graph --

    async def get_graph(self, scope: TenantProjectScope) -> dict[str, Any]:
        """Authoritative graph view; offline and always available."""
        graph = await self._authority.load_graph(scope)
        if graph is None:
            raise AnalysisError("story_bible_not_found", 404)
        return self._graph_dict(graph)

    async def sync_scope(self, scope: TenantProjectScope) -> dict[str, Any]:
        """Project the authoritative graph into Neo4j; does not touch the authority."""
        if self._projection is None:
            raise AnalysisError("graph_unconfigured", 503)
        graph = await self._authority.load_graph(scope)
        if graph is None:
            raise AnalysisError("story_bible_not_found", 404)
        try:
            await self._projection.rebuild_scope(graph)
        except GraphProjectionError as exc:
            raise AnalysisError(f"graph_projection_{exc.code}", 503) from None
        return {"projected": True}

    @staticmethod
    def _graph_dict(graph: AuthoritativeGraph) -> dict[str, Any]:
        return {
            "tenant_id": str(graph.tenant_id),
            "project_id": str(graph.project_id),
            "characters": [
                {
                    "id": str(c.character_id),
                    "name": c.name,
                    "aliases": list(c.aliases),
                    "description": list(c.description),
                    "traits": list(c.traits),
                }
                for c in graph.characters
            ],
            "events": [
                {
                    "id": str(e.event_id),
                    "title": e.title,
                    "description": e.description,
                    "occurred_at": e.occurred_at.isoformat() if e.occurred_at else None,
                    "location": e.location,
                    "event_type": e.event_type,
                    "outcome": e.outcome,
                }
                for e in graph.events
            ],
            "relations": [
                {
                    "id": str(r.relation_id),
                    "relation_type": r.relation_type,
                    "from_character_id": str(r.from_character_id),
                    "to_character_id": str(r.to_character_id),
                    "description": r.description,
                }
                for r in graph.relations
            ],
            "participations": [
                {
                    "event_id": str(p.event_id),
                    "character_id": str(p.character_id),
                    "role": p.role,
                    "order_no": p.order_no,
                }
                for p in graph.participations
            ],
        }


__all__ = [
    "EventCreate",
    "EventParticipant",
    "EventPatch",
    "RelationCreate",
    "RelationPatch",
    "StoryGraphService",
]
