import uuid
from datetime import UTC, datetime
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

RequestId = Annotated[str, StringConstraints(min_length=1, max_length=100)]
ActorId = Annotated[str, StringConstraints(min_length=1, max_length=255)]
ProjectTitle = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)
]


class ProjectCreatedV1(BaseModel):
    """Immutable integration event emitted when a project is first created."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    event_type: Literal["project.created.v1"] = "project.created.v1"
    event_id: uuid.UUID
    tenant_id: uuid.UUID
    project_id: uuid.UUID
    branch_id: uuid.UUID
    actor_id: ActorId
    request_id: RequestId
    title: ProjectTitle
    language: Literal["zh-CN"]
    version: Literal[1] = 1
    occurred_at: datetime = Field(default=datetime(1970, 1, 1, tzinfo=UTC), exclude=True)

    @property
    def aggregate_type(self) -> str:
        return "project"

    @property
    def aggregate_id(self) -> uuid.UUID:
        return self.project_id

    @property
    def aggregate_version(self) -> int:
        return self.version


class IntegrationEvent(Protocol):
    """Closed structural boundary accepted by authoritative outbox persistence."""

    @property
    def event_id(self) -> uuid.UUID: ...

    @property
    def tenant_id(self) -> uuid.UUID: ...

    @property
    def project_id(self) -> uuid.UUID: ...

    @property
    def event_type(self) -> str: ...

    @property
    def occurred_at(self) -> datetime: ...

    @property
    def aggregate_type(self) -> str: ...

    @property
    def aggregate_id(self) -> uuid.UUID: ...

    @property
    def aggregate_version(self) -> int: ...

    def model_dump(self, *, mode: Literal["json"]) -> dict[str, Any]: ...
