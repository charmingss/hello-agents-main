"""Story Graph transport port: fake in-memory store and lazy official Neo4j adapter."""

from __future__ import annotations

import importlib
import inspect
import uuid
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, cast

from novel_agent.graph.schema import (
    GRAPH_SCHEMA_VERSION,
    CharacterNode,
    EdgeType,
    EventNode,
    NodeLabel,
    ParticipationEdge,
    RelationEdge,
    deterministic_character_node_id,
    deterministic_event_id,
    deterministic_participation_id,
    deterministic_relation_id,
)
from novel_agent.vector.schema import TenantProjectScope

GraphErrorCode = Literal[
    "graph_unavailable",
    "graph_schema_conflict",
    "invalid_configuration",
    "graph_driver_missing",
    "graph_installation_broken",
    "graph_scope_mismatch",
]


class GraphProjectionError(RuntimeError):
    """Stable, redacted graph projection error."""

    def __init__(self, code: GraphErrorCode) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class GraphNode:
    id: uuid.UUID
    label: NodeLabel
    properties: Mapping[str, Any]


@dataclass(frozen=True)
class GraphEdge:
    id: uuid.UUID
    edge_type: EdgeType
    from_id: uuid.UUID
    to_id: uuid.UUID
    properties: Mapping[str, Any]


class GraphTransport(Protocol):
    """Small semantic port implemented by the in-memory fake or the official driver adapter."""

    async def upsert_node(self, node: GraphNode) -> None: ...

    async def upsert_relationship(self, edge: GraphEdge) -> None: ...

    async def delete_scope(self, scope: TenantProjectScope) -> None: ...

    async def query_nodes(
        self, scope: TenantProjectScope, *, label: NodeLabel
    ) -> tuple[GraphNode, ...]: ...

    async def query_relationships(
        self, scope: TenantProjectScope, *, edge_type: EdgeType
    ) -> tuple[GraphEdge, ...]: ...

    async def aclose(self) -> None: ...


@dataclass
class FakeGraphStore:
    """In-memory graph store for offline tests and tooling."""

    nodes: list[GraphNode] = field(default_factory=list)
    edges: list[GraphEdge] = field(default_factory=list)
    closed: bool = False

    def _require_open(self) -> None:
        if self.closed:
            raise GraphProjectionError("invalid_configuration")

    async def upsert_node(self, node: GraphNode) -> None:
        self._require_open()
        self.nodes = [n for n in self.nodes if n.id != node.id] + [node]

    async def upsert_relationship(self, edge: GraphEdge) -> None:
        self._require_open()
        self.edges = [e for e in self.edges if e.id != edge.id] + [edge]

    async def delete_scope(self, scope: TenantProjectScope) -> None:
        self._require_open()
        self.nodes = [
            n
            for n in self.nodes
            if n.properties.get("tenant_id") != str(scope.tenant_id)
            or n.properties.get("project_id") != str(scope.project_id)
        ]
        self.edges = [
            e
            for e in self.edges
            if e.properties.get("tenant_id") != str(scope.tenant_id)
            or e.properties.get("project_id") != str(scope.project_id)
        ]

    async def query_nodes(
        self, scope: TenantProjectScope, *, label: NodeLabel
    ) -> tuple[GraphNode, ...]:
        self._require_open()
        return tuple(
            node
            for node in self.nodes
            if node.label == label
            and node.properties.get("tenant_id") == str(scope.tenant_id)
            and node.properties.get("project_id") == str(scope.project_id)
        )

    async def query_relationships(
        self, scope: TenantProjectScope, *, edge_type: EdgeType
    ) -> tuple[GraphEdge, ...]:
        self._require_open()
        return tuple(
            edge
            for edge in self.edges
            if edge.edge_type == edge_type
            and edge.properties.get("tenant_id") == str(scope.tenant_id)
            and edge.properties.get("project_id") == str(scope.project_id)
        )

    async def aclose(self) -> None:
        self.closed = True


def graph_node_from_character(node: CharacterNode) -> GraphNode:
    return GraphNode(
        id=deterministic_character_node_id(node, schema_version=GRAPH_SCHEMA_VERSION),
        label="Character",
        properties={
            "tenant_id": str(node.tenant_id),
            "project_id": str(node.project_id),
            "character_id": str(node.character_id),
            "name": node.name,
            "aliases": list(node.aliases),
            "description": list(node.description),
            "traits": list(node.traits),
        },
    )


def graph_node_from_event(node: EventNode) -> GraphNode:
    return GraphNode(
        id=deterministic_event_id(node, schema_version=GRAPH_SCHEMA_VERSION),
        label="Event",
        properties={
            "tenant_id": str(node.tenant_id),
            "project_id": str(node.project_id),
            "event_id": str(node.event_id),
            "title": node.title,
            "description": node.description,
            "occurred_at": node.occurred_at.isoformat() if node.occurred_at else None,
            "location": node.location,
            "event_type": node.event_type,
            "outcome": node.outcome,
        },
    )


def graph_edge_from_relation(edge: RelationEdge) -> GraphEdge:
    return GraphEdge(
        id=deterministic_relation_id(edge, schema_version=GRAPH_SCHEMA_VERSION),
        edge_type="RELATES_TO",
        from_id=deterministic_character_node_id(
            CharacterNode(
                tenant_id=edge.tenant_id,
                project_id=edge.project_id,
                character_id=edge.from_character_id,
                name="",
            ),
            schema_version=GRAPH_SCHEMA_VERSION,
        ),
        to_id=deterministic_character_node_id(
            CharacterNode(
                tenant_id=edge.tenant_id,
                project_id=edge.project_id,
                character_id=edge.to_character_id,
                name="",
            ),
            schema_version=GRAPH_SCHEMA_VERSION,
        ),
        properties={
            "tenant_id": str(edge.tenant_id),
            "project_id": str(edge.project_id),
            "relation_id": str(edge.relation_id),
            "relation_type": edge.relation_type,
            "description": edge.description,
        },
    )


def graph_edge_from_participation(edge: ParticipationEdge) -> GraphEdge:
    return GraphEdge(
        id=deterministic_participation_id(edge, schema_version=GRAPH_SCHEMA_VERSION),
        edge_type="PARTICIPATES_IN",
        from_id=deterministic_character_node_id(
            CharacterNode(
                tenant_id=edge.tenant_id,
                project_id=edge.project_id,
                character_id=edge.character_id,
                name="",
            ),
            schema_version=GRAPH_SCHEMA_VERSION,
        ),
        to_id=deterministic_event_id(
            EventNode(
                tenant_id=edge.tenant_id,
                project_id=edge.project_id,
                event_id=edge.event_id,
                title="",
                description="",
            ),
            schema_version=GRAPH_SCHEMA_VERSION,
        ),
        properties={
            "tenant_id": str(edge.tenant_id),
            "project_id": str(edge.project_id),
            "event_id": str(edge.event_id),
            "character_id": str(edge.character_id),
            "role": edge.role,
            "order_no": edge.order_no,
        },
    )


class OfficialNeo4jGraphTransport:
    """Lazy adapter: importing this module does not require or configure neo4j."""

    def __init__(self, driver: Any) -> None:
        self._driver = driver
        required_async_methods = ("execute_query", "close")
        if any(
            not inspect.iscoroutinefunction(getattr(driver, method, None))
            for method in required_async_methods
        ):
            raise GraphProjectionError("invalid_configuration")
        self._closed = False

    async def upsert_node(self, node: GraphNode) -> None:
        del node
        raise GraphProjectionError("graph_unavailable")

    async def upsert_relationship(self, edge: GraphEdge) -> None:
        del edge
        raise GraphProjectionError("graph_unavailable")

    async def delete_scope(self, scope: TenantProjectScope) -> None:
        del scope
        raise GraphProjectionError("graph_unavailable")

    async def query_nodes(
        self, scope: TenantProjectScope, *, label: NodeLabel
    ) -> tuple[GraphNode, ...]:
        del scope, label
        return ()

    async def query_relationships(
        self, scope: TenantProjectScope, *, edge_type: EdgeType
    ) -> tuple[GraphEdge, ...]:
        del scope, edge_type
        return ()

    async def aclose(self) -> None:
        if self._closed:
            return
        try:
            await self._driver.close()
        except Exception:
            raise GraphProjectionError("graph_unavailable") from None
        self._closed = True


def create_official_neo4j_transport(
    *, uri: str, user: str, password: str, database: str, timeout: float = 10.0
) -> OfficialNeo4jGraphTransport:
    """Composition helper that imports the neo4j driver only when explicitly selected."""
    if not uri.strip() or not user.strip() or not database.strip():
        raise GraphProjectionError("invalid_configuration")
    package = _import_neo4j_module("neo4j")
    try:
        driver = package.GraphDatabase.driver(
            uri,
            auth=(user, password),
            connection_timeout=timeout,
        )
    except Exception:
        failed = True
    else:
        failed = False
    if failed:
        raise GraphProjectionError("invalid_configuration") from None
    return OfficialNeo4jGraphTransport(cast(Any, driver))


def _import_neo4j_module(name: str) -> Any:
    try:
        module = importlib.import_module(name)
    except ModuleNotFoundError as error:
        code: GraphErrorCode = (
            "graph_driver_missing" if error.name == "neo4j" else "graph_installation_broken"
        )
    except ImportError:
        code = "graph_installation_broken"
    else:
        return module
    raise GraphProjectionError(code) from None


__all__ = [
    "FakeGraphStore",
    "GraphEdge",
    "GraphErrorCode",
    "GraphNode",
    "GraphProjectionError",
    "GraphTransport",
    "OfficialNeo4jGraphTransport",
    "create_official_neo4j_transport",
    "graph_edge_from_participation",
    "graph_edge_from_relation",
    "graph_node_from_character",
    "graph_node_from_event",
]
