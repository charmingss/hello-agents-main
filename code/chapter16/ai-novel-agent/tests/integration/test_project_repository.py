import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from novel_agent.db.models.identity import Tenant
from novel_agent.db.models.outbox import OutboxEvent
from novel_agent.db.models.project import StoryBranch
from novel_agent.projects.repository import SqlAlchemyProjectRepository
from novel_agent.projects.schemas import CreateProject

pytestmark = pytest.mark.external


async def test_repository_scopes_project_reads_to_tenant(db_session) -> None:
    tenant_a = Tenant(id=uuid.uuid4(), slug="tenant-a", name="Tenant A")
    tenant_b = Tenant(id=uuid.uuid4(), slug="tenant-b", name="Tenant B")
    db_session.add_all([tenant_a, tenant_b])
    await db_session.flush()

    repository = SqlAlchemyProjectRepository(db_session)
    created = await repository.create(
        tenant_a.id,
        CreateProject(request_id="repo-test-1", title="长夜", language="zh-CN"),
    )

    assert await repository.get(tenant_a.id, created.id) == created
    assert await repository.get(tenant_b.id, created.id) is None


async def test_repository_create_is_idempotent_by_tenant_request(db_session) -> None:
    tenant = Tenant(id=uuid.uuid4(), slug="idempotent", name="Idempotent")
    db_session.add(tenant)
    await db_session.flush()
    repository = SqlAlchemyProjectRepository(db_session)
    request = CreateProject(request_id="same-request", title="长夜")

    first = await repository.create(tenant.id, request)
    second = await repository.create(tenant.id, request)

    assert second.id == first.id


async def test_story_branch_rejects_cross_tenant_project_reference(db_session) -> None:
    tenant_a = Tenant(id=uuid.uuid4(), slug="branch-a", name="Branch A")
    tenant_b = Tenant(id=uuid.uuid4(), slug="branch-b", name="Branch B")
    db_session.add_all([tenant_a, tenant_b])
    await db_session.flush()
    project = await SqlAlchemyProjectRepository(db_session).create(
        tenant_a.id, CreateProject(request_id="branch-project", title="长夜")
    )
    db_session.add(
        StoryBranch(
            id=uuid.uuid4(),
            tenant_id=tenant_a.id,
            project_id=project.id,
            name="main",
            is_default=True,
        )
    )
    await db_session.flush()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                StoryBranch(
                    id=uuid.uuid4(),
                    tenant_id=tenant_b.id,
                    project_id=project.id,
                    name="invalid",
                )
            )
            await db_session.flush()


async def test_outbox_rejects_cross_tenant_project_reference(db_session) -> None:
    tenant_a = Tenant(id=uuid.uuid4(), slug="outbox-a", name="Outbox A")
    tenant_b = Tenant(id=uuid.uuid4(), slug="outbox-b", name="Outbox B")
    db_session.add_all([tenant_a, tenant_b])
    await db_session.flush()
    project = await SqlAlchemyProjectRepository(db_session).create(
        tenant_a.id, CreateProject(request_id="outbox-project", title="长夜")
    )
    aggregate_id = uuid.uuid4()
    db_session.add(
        OutboxEvent(
            tenant_id=tenant_a.id,
            project_id=project.id,
            aggregate_type="project",
            aggregate_id=aggregate_id,
            event_type="project.created",
            aggregate_version=1,
            payload={},
        )
    )
    await db_session.flush()

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                OutboxEvent(
                    tenant_id=tenant_b.id,
                    project_id=project.id,
                    aggregate_type="project",
                    aggregate_id=aggregate_id,
                    event_type="project.created",
                    aggregate_version=1,
                    payload={},
                )
            )
            await db_session.flush()
