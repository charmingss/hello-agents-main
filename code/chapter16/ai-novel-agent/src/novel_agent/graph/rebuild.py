"""Graph rebuild orchestrator: page over canonical scopes and replay the projection."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novel_agent.db.models.story_bible import StoryBible
from novel_agent.graph.authority import GraphAuthority
from novel_agent.graph.projection import GraphProjection
from novel_agent.vector.schema import TenantProjectScope


class GraphScopeAuthority(Protocol):
    async def page_scopes(
        self,
        scope: TenantProjectScope | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[tuple[TenantProjectScope, ...], str | None]: ...


class SqlAlchemyGraphScopeAuthority:
    """Pages the canonical scopes that have a story bible."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def page_scopes(
        self,
        scope: TenantProjectScope | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[tuple[TenantProjectScope, ...], str | None]:
        if not 1 <= limit <= 500:
            raise ValueError("graph rebuild page limit must be between 1 and 500")
        after = None
        if cursor is not None:
            try:
                after = uuid.UUID(cursor)
            except ValueError:
                raise ValueError("invalid graph rebuild cursor") from None
        statement = (
            select(StoryBible.id, StoryBible.tenant_id, StoryBible.project_id)
            .order_by(StoryBible.id)
            .limit(limit + 1)
        )
        if scope is not None:
            statement = statement.where(
                StoryBible.tenant_id == scope.tenant_id,
                StoryBible.project_id == scope.project_id,
            )
        if after is not None:
            statement = statement.where(StoryBible.id > after)
        async with self._session_factory() as session:
            rows = (await session.execute(statement)).all()
            selected = rows[:limit]
            scopes = tuple(
                TenantProjectScope(tenant_id=tenant, project_id=project)
                for _, tenant, project in selected
            )
        if scope is not None and any(item != scope for item in scopes):
            raise RuntimeError("graph_rebuild_scope_mismatch")
        next_cursor = str(rows[-1][0]) if len(rows) > limit and scopes else None
        return scopes, next_cursor


class GraphRebuilder:
    """Keyset-paged graph rebuild. Scope is retained on every authority and projection call."""

    def __init__(
        self,
        scope_authority: GraphScopeAuthority,
        authority: GraphAuthority,
        projection: GraphProjection,
        *,
        page_size: int = 50,
    ) -> None:
        if not 1 <= page_size <= 500:
            raise ValueError("graph rebuild page size must be between 1 and 500")
        self._scope_authority = scope_authority
        self._authority = authority
        self._projection = projection
        self._page_size = page_size

    async def rebuild_project(
        self,
        scope: TenantProjectScope,
        *,
        canceled: Callable[[], bool] = lambda: False,
        heartbeat: Callable[[int], None] = lambda count: None,
    ) -> int:
        if not isinstance(scope, TenantProjectScope):
            raise ValueError("valid tenant project scope is required")
        return await self._run(scope, canceled=canceled, heartbeat=heartbeat)

    async def rebuild_all(
        self,
        *,
        canceled: Callable[[], bool] = lambda: False,
        heartbeat: Callable[[int], None] = lambda count: None,
    ) -> int:
        return await self._run(None, canceled=canceled, heartbeat=heartbeat)

    async def _run(
        self,
        scope: TenantProjectScope | None,
        *,
        canceled: Callable[[], bool],
        heartbeat: Callable[[int], None],
    ) -> int:
        cursor: str | None = None
        rebuilt = 0
        while True:
            if canceled():
                raise asyncio.CancelledError
            scopes, next_cursor = await self._scope_authority.page_scopes(
                scope, cursor, self._page_size
            )
            if scope is not None and any(item != scope for item in scopes):
                raise RuntimeError("graph_rebuild_scope_mismatch")
            for item in scopes:
                if canceled():
                    raise asyncio.CancelledError
                graph = await self._authority.load_graph(item)
                if graph is None:
                    continue
                if graph.scope != item:
                    raise RuntimeError("graph_rebuild_scope_mismatch")
                await self._projection.rebuild_scope(graph)
                rebuilt += 1
                heartbeat(rebuilt)
            if next_cursor is None:
                return rebuilt
            if next_cursor == cursor:
                raise RuntimeError("graph_rebuild_cursor_did_not_advance")
            cursor = next_cursor


__all__ = [
    "GraphRebuilder",
    "GraphScopeAuthority",
    "SqlAlchemyGraphScopeAuthority",
]