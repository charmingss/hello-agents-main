"""Graph projection adapter: writes a full authoritative snapshot into a Neo4j scope."""

from __future__ import annotations

from typing import Protocol

from novel_agent.graph.schema import AuthoritativeGraph
from novel_agent.graph.transport import (
    GraphTransport,
    graph_edge_from_participation,
    graph_edge_from_relation,
    graph_node_from_character,
    graph_node_from_event,
)
from novel_agent.vector.schema import TenantProjectScope


class GraphProjection(Protocol):
    """Projection port: write an authoritative graph into a graph scope."""

    async def upsert_all(self, graph: AuthoritativeGraph) -> None: ...

    async def rebuild_scope(self, graph: AuthoritativeGraph) -> None: ...

    async def delete_scope(self, scope: TenantProjectScope) -> None: ...


class Neo4jGraphProjection:
    """A single transport-backed projection. The scope is forced from the graph itself."""

    def __init__(self, transport: GraphTransport) -> None:
        self._transport = transport

    async def upsert_all(self, graph: AuthoritativeGraph) -> None:
        """Write/overwrite all nodes and edges for the graph's scope.

        The transport stores tenant/project inside each element, so a stale
        caller-supplied scope cannot leak across tenants.
        """
        for character in graph.characters:
            await self._transport.upsert_node(graph_node_from_character(character))
        for event in graph.events:
            await self._transport.upsert_node(graph_node_from_event(event))
        for relation in graph.relations:
            await self._transport.upsert_relationship(graph_edge_from_relation(relation))
        for participation in graph.participations:
            await self._transport.upsert_relationship(
                graph_edge_from_participation(participation)
            )

    async def rebuild_scope(self, graph: AuthoritativeGraph) -> None:
        """Atomically rebuild the scope: delete everything, then upsert the graph."""
        await self._transport.delete_scope(graph.scope)
        await self.upsert_all(graph)

    async def delete_scope(self, scope: TenantProjectScope) -> None:
        await self._transport.delete_scope(scope)

    async def aclose(self) -> None:
        await self._transport.aclose()


__all__ = ["GraphProjection", "Neo4jGraphProjection"]