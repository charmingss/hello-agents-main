"""Graph authority: reads the canonical story graph out of PostgreSQL."""

from __future__ import annotations

from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novel_agent.db.models.story_bible import Character, StoryBible
from novel_agent.db.models.story_graph import (
    StoryEvent,
    StoryRelation,
)
from novel_agent.graph.schema import (
    AuthoritativeGraph,
    CharacterNode,
    EventNode,
    ParticipationEdge,
    RelationEdge,
)
from novel_agent.vector.schema import TenantProjectScope


class GraphAuthority(Protocol):
    async def load_graph(self, scope: TenantProjectScope) -> AuthoritativeGraph | None: ...


class SqlAlchemyGraphAuthority:
    """Materializes the full authoritative graph for a tenant/project scope."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def load_graph(self, scope: TenantProjectScope) -> AuthoritativeGraph | None:
        """Return None when the project has no story bible yet (nothing to project)."""
        async with self._session_factory() as session:
            bible = await session.scalar(
                select(StoryBible).where(
                    StoryBible.tenant_id == scope.tenant_id,
                    StoryBible.project_id == scope.project_id,
                )
            )
            if bible is None:
                return None
            characters = tuple(
                CharacterNode(
                    tenant_id=character.tenant_id,
                    project_id=character.project_id,
                    character_id=character.id,
                    name=character.name,
                    aliases=tuple(character.aliases),
                    description=tuple(character.description),
                    traits=tuple(character.traits),
                )
                for character in (
                    await session.scalars(
                        select(Character)
                        .where(
                            Character.tenant_id == scope.tenant_id,
                            Character.project_id == scope.project_id,
                            Character.story_bible_id == bible.id,
                        )
                        .order_by(Character.created_at, Character.id)
                    )
                ).all()
            )
            event_rows = (
                await session.scalars(
                    select(StoryEvent)
                    .where(
                        StoryEvent.tenant_id == scope.tenant_id,
                        StoryEvent.project_id == scope.project_id,
                        StoryEvent.story_bible_id == bible.id,
                    )
                    .order_by(StoryEvent.created_at, StoryEvent.id)
                )
            ).all()
            events = tuple(
                EventNode(
                    tenant_id=event.tenant_id,
                    project_id=event.project_id,
                    event_id=event.id,
                    title=event.title,
                    description=event.description,
                    occurred_at=event.occurred_at,
                    location=event.location,
                    event_type=event.event_type,
                    outcome=event.outcome,
                )
                for event in event_rows
            )
            relations = list(
                RelationEdge(
                    tenant_id=relation.tenant_id,
                    project_id=relation.project_id,
                    relation_id=relation.id,
                    relation_type=relation.relation_type,
                    from_character_id=relation.from_character_id,
                    to_character_id=relation.to_character_id,
                    description=relation.description,
                )
                for relation in (
                    await session.scalars(
                        select(StoryRelation)
                        .where(
                            StoryRelation.tenant_id == scope.tenant_id,
                            StoryRelation.project_id == scope.project_id,
                            StoryRelation.story_bible_id == bible.id,
                        )
                        .order_by(StoryRelation.created_at, StoryRelation.id)
                    )
                ).all()
            )
            participations: list[ParticipationEdge] = []
            for event in event_rows:
                for index, participant in enumerate(event.participants or []):
                    participations.append(
                        ParticipationEdge(
                            tenant_id=event.tenant_id,
                            project_id=event.project_id,
                            event_id=event.id,
                            character_id=participant["character_id"],
                            role=participant["role"],
                            order_no=participant.get("order_index", index),
                        )
                    )
            return AuthoritativeGraph(
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                characters=characters,
                events=tuple(events),
                relations=tuple(relations),
                participations=tuple(participations),
            )


__all__ = ["GraphAuthority", "SqlAlchemyGraphAuthority"]
