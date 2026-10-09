import uuid

import pytest

from novel_agent.db.models.project import Project, StoryBranch
from novel_agent.outbox.events import ProjectCreatedV1
from novel_agent.projects.schemas import CreateProject
from novel_agent.projects.service import ProjectService


class FakeProjectRepository:
    def __init__(self) -> None:
        self.project = Project()
        self.branch = StoryBranch()
        self.commit_calls = 0

    async def create(self, tenant_id: uuid.UUID, request: CreateProject) -> Project:
        self.project.id = uuid.uuid4()
        self.project.tenant_id = tenant_id
        self.project.creation_request_id = request.request_id
        self.project.slug = "long-night"
        self.project.title = request.title
        self.project.language = request.language
        return self.project

    async def create_default_branch(
        self, tenant_id: uuid.UUID, project_id: uuid.UUID
    ) -> StoryBranch:
        self.branch.id = uuid.uuid4()
        self.branch.tenant_id = tenant_id
        self.branch.project_id = project_id
        self.branch.name = "main"
        self.branch.is_default = True
        return self.branch

    async def commit(self) -> None:
        self.commit_calls += 1


class FakeOutboxRepository:
    def __init__(self) -> None:
        self.events: list[ProjectCreatedV1] = []
        self.commit_calls = 0

    async def add(self, event: ProjectCreatedV1) -> None:
        self.events.append(event)

    async def commit(self) -> None:
        self.commit_calls += 1


@pytest.mark.asyncio
async def test_create_project_adds_default_branch_and_project_created_event() -> None:
    tenant_id = uuid.uuid4()
    actor_id = "oidc|user-1"
    projects = FakeProjectRepository()
    outbox = FakeOutboxRepository()
    service = ProjectService(projects, outbox)

    created = await service.create(
        tenant_id,
        actor_id,
        CreateProject(request_id="req-1", title="长夜", language="zh-CN"),
    )

    assert created.default_branch.name == "main"
    assert len(outbox.events) == 1
    event = outbox.events[0]
    assert event.event_type == "project.created.v1"
    assert event.tenant_id == tenant_id
    assert event.project_id == created.project.id
    assert event.branch_id == created.default_branch.id
    assert event.actor_id == actor_id
    assert event.request_id == "req-1"
    assert projects.commit_calls == 0
    assert outbox.commit_calls == 0
