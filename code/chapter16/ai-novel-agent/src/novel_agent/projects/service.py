import uuid
from datetime import UTC, datetime
from typing import Protocol

from novel_agent.db.models.project import Project, StoryBranch
from novel_agent.outbox.events import ProjectCreatedV1
from novel_agent.projects.schemas import CreatedProject, CreateProject


class ProjectRepository(Protocol):
    async def create(self, tenant_id: uuid.UUID, request: CreateProject) -> Project: ...

    async def create_default_branch(
        self, tenant_id: uuid.UUID, project_id: uuid.UUID
    ) -> StoryBranch: ...

    async def get_by_request_id(
        self, tenant_id: uuid.UUID, request_id: str
    ) -> CreatedProject | None: ...

    async def get_with_default_branch(
        self, tenant_id: uuid.UUID, project_id: uuid.UUID
    ) -> CreatedProject | None: ...

    async def list_with_default_branch(self, tenant_id: uuid.UUID) -> list[CreatedProject]: ...


class OutboxRepository(Protocol):
    async def add(self, event: ProjectCreatedV1) -> object: ...


class ProjectService:
    def __init__(self, projects: ProjectRepository, outbox: OutboxRepository) -> None:
        self._projects = projects
        self._outbox = outbox

    async def create(
        self, tenant_id: uuid.UUID, actor_id: str, request: CreateProject
    ) -> CreatedProject:
        project = await self._projects.create(tenant_id, request)
        branch = await self._projects.create_default_branch(tenant_id, project.id)
        event = ProjectCreatedV1(
            event_id=uuid.uuid4(),
            tenant_id=tenant_id,
            project_id=project.id,
            branch_id=branch.id,
            actor_id=actor_id,
            request_id=request.request_id,
            title=project.title,
            language=request.language,
            occurred_at=datetime.now(UTC),
        )
        await self._outbox.add(event)
        return CreatedProject(project=project, default_branch=branch)

    async def get(self, tenant_id: uuid.UUID, project_id: uuid.UUID) -> CreatedProject | None:
        return await self._projects.get_with_default_branch(tenant_id, project_id)

    async def list(self, tenant_id: uuid.UUID) -> list[CreatedProject]:
        return await self._projects.list_with_default_branch(tenant_id)
