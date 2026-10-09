"""Story Graph projection schema: deterministic node/edge identities and payloads."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

from novel_agent.vector.schema import TenantProjectScope

GRAPH_SCHEMA_VERSION = 1
GRAPH_ID_NAMESPACE = uuid.UUID("3d7c64a9-6d1a-4b02-8b13-2c18a4f7d9e0")

NodeLabel = Literal["Character", "Event"]
EdgeType = Literal["RELATES_TO", "PARTICIPATES_IN"]

RelationType = Annotated[str, StringConstraints(min_length=1, max_length=50)]
RelationDescription = Annotated[str, StringConstraints(min_length=1, max_length=500)]
EventTitle = Annotated[str, StringConstraints(min_length=1, max_length=200)]
EventDescription = Annotated[str, StringConstraints(min_length=1, max_length=2000)]
EventLocation = Annotated[str, StringConstraints(max_length=500)]
EventRole = Annotated[str, StringConstraints(min_length=1, max_length=50)]


class CharacterNode(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    tenant_id: uuid.UUID
    project_id: uuid.UUID
    character_id: uuid.UUID
    name: str
    aliases: tuple[str, ...] = ()
    description: tuple[str, ...] = ()
    traits: tuple[str, ...] = ()


class EventNode(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    tenant_id: uuid.UUID
    project_id: uuid.UUID
    event_id: uuid.UUID
    title: str
    description: str
    occurred_at: datetime | None = None
    location: str | None = None
    event_type: str | None = None
    outcome: str | None = None


class RelationEdge(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    tenant_id: uuid.UUID
    project_id: uuid.UUID
    relation_id: uuid.UUID
    relation_type: RelationType
    from_character_id: uuid.UUID
    to_character_id: uuid.UUID
    description: RelationDescription

    @model_validator(mode="after")
    def reject_self_relation(self) -> RelationEdge:
        if self.from_character_id == self.to_character_id:
            raise ValueError("a character cannot relate to itself")
        return self


class ParticipationEdge(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    tenant_id: uuid.UUID
    project_id: uuid.UUID
    event_id: uuid.UUID
    character_id: uuid.UUID
    role: EventRole
    order_no: Annotated[int, Field(ge=0)]


class AuthoritativeGraph(BaseModel):
    """Self-describing projection input; PostgreSQL remains its authority."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    tenant_id: uuid.UUID
    project_id: uuid.UUID
    characters: tuple[CharacterNode, ...] = ()
    events: tuple[EventNode, ...] = ()
    relations: tuple[RelationEdge, ...] = ()
    participations: tuple[ParticipationEdge, ...] = ()

    @property
    def scope(self) -> TenantProjectScope:
        return TenantProjectScope(tenant_id=self.tenant_id, project_id=self.project_id)


def _scope_material(scope: TenantProjectScope) -> dict[str, str]:
    return {"tenant_id": str(scope.tenant_id), "project_id": str(scope.project_id)}


def _scope_from_fields(tenant_id: uuid.UUID, project_id: uuid.UUID) -> TenantProjectScope:
    return TenantProjectScope(tenant_id=tenant_id, project_id=project_id)


def deterministic_character_node_id(node: CharacterNode, *, schema_version: int) -> uuid.UUID:
    if schema_version <= 0:
        raise ValueError("schema_version must be positive")
    material = {
        "scope": _scope_material(_scope_from_fields(node.tenant_id, node.project_id)),
        "character_id": str(node.character_id),
        "schema_version": schema_version,
    }
    return uuid.uuid5(
        GRAPH_ID_NAMESPACE,
        json.dumps(material, sort_keys=True, separators=(",", ":")),
    )


def deterministic_event_id(node: EventNode, *, schema_version: int) -> uuid.UUID:
    if schema_version <= 0:
        raise ValueError("schema_version must be positive")
    material = {
        "scope": _scope_material(_scope_from_fields(node.tenant_id, node.project_id)),
        "event_id": str(node.event_id),
        "schema_version": schema_version,
    }
    return uuid.uuid5(
        GRAPH_ID_NAMESPACE,
        json.dumps(material, sort_keys=True, separators=(",", ":")),
    )


def deterministic_relation_id(edge: RelationEdge, *, schema_version: int) -> uuid.UUID:
    if schema_version <= 0:
        raise ValueError("schema_version must be positive")
    material = {
        "scope": _scope_material(_scope_from_fields(edge.tenant_id, edge.project_id)),
        "relation_id": str(edge.relation_id),
        "schema_version": schema_version,
    }
    return uuid.uuid5(
        GRAPH_ID_NAMESPACE,
        json.dumps(material, sort_keys=True, separators=(",", ":")),
    )


def deterministic_participation_id(edge: ParticipationEdge, *, schema_version: int) -> uuid.UUID:
    if schema_version <= 0:
        raise ValueError("schema_version must be positive")
    material = {
        "scope": _scope_material(_scope_from_fields(edge.tenant_id, edge.project_id)),
        "event_id": str(edge.event_id),
        "character_id": str(edge.character_id),
        "schema_version": schema_version,
    }
    return uuid.uuid5(
        GRAPH_ID_NAMESPACE,
        json.dumps(material, sort_keys=True, separators=(",", ":")),
    )


def scope_partition(scope: TenantProjectScope) -> str:
    """A stable Neo4j database/partition name for a tenant/project scope."""
    digest = hashlib.sha256(
        json.dumps(_scope_material(scope), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:24]
    slug = re.sub(
        r"[^a-z0-9]+",
        "_",
        f"{scope.tenant_id}_{scope.project_id}".lower(),
    ).strip("_")[:40]
    return f"novel_graph_v{GRAPH_SCHEMA_VERSION}_{slug}_{digest}"


__all__ = [
    "GRAPH_ID_NAMESPACE",
    "GRAPH_SCHEMA_VERSION",
    "AuthoritativeGraph",
    "CharacterNode",
    "EdgeType",
    "EventNode",
    "NodeLabel",
    "ParticipationEdge",
    "RelationEdge",
    "deterministic_character_node_id",
    "deterministic_event_id",
    "deterministic_participation_id",
    "deterministic_relation_id",
    "scope_partition",
]
