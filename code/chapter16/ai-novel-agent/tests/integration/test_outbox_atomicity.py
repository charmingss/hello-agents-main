import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from conftest import postgres_test_database_url
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from novel_agent.db.models.identity import Tenant
from novel_agent.db.models.outbox import OutboxEvent
from novel_agent.db.models.project import Project, StoryBranch
from novel_agent.outbox.repository import SqlAlchemyOutboxRepository
from novel_agent.projects.repository import SqlAlchemyProjectRepository
from novel_agent.projects.schemas import CreateProject
from novel_agent.projects.service import ProjectService

pytestmark = pytest.mark.external


@pytest_asyncio.fixture
async def database_engine() -> AsyncIterator[AsyncEngine]:
    engine = create_async_engine(postgres_test_database_url(), pool_pre_ping=True)
    try:
        yield engine
    finally:
        await engine.dispose()


async def _insert_tenant(factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID) -> None:
    async with factory.begin() as session:
        session.add(Tenant(id=tenant_id, slug=f"tenant-{tenant_id.hex}", name="Atomicity Test"))


async def _delete_tenant_data(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    async with factory.begin() as session:
        await session.execute(delete(OutboxEvent).where(OutboxEvent.tenant_id == tenant_id))
        await session.execute(delete(StoryBranch).where(StoryBranch.tenant_id == tenant_id))
        await session.execute(delete(Project).where(Project.tenant_id == tenant_id))
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


@pytest.mark.asyncio
async def test_project_and_outbox_are_rolled_back_together(database_engine: AsyncEngine) -> None:
    tenant_id = uuid.uuid4()
    actor_id = "oidc|rollback-user"
    request_id = f"rollback-{uuid.uuid4()}"
    factory = async_sessionmaker(database_engine, expire_on_commit=False)
    await _insert_tenant(factory, tenant_id)

    try:
        with pytest.raises(RuntimeError, match="force rollback"):
            async with factory() as session, session.begin():
                service = ProjectService(
                    SqlAlchemyProjectRepository(session), SqlAlchemyOutboxRepository(session)
                )
                await service.create(
                    tenant_id,
                    actor_id,
                    CreateProject(request_id=request_id, title="长夜", language="zh-CN"),
                )
                raise RuntimeError("force rollback")

        async with factory() as session:
            project = await session.scalar(
                select(Project).where(
                    Project.tenant_id == tenant_id,
                    Project.creation_request_id == request_id,
                )
            )
            outbox = await session.scalar(
                select(OutboxEvent).where(OutboxEvent.tenant_id == tenant_id)
            )
            branch = await session.scalar(
                select(StoryBranch).where(StoryBranch.tenant_id == tenant_id)
            )
        assert project is None
        assert branch is None
        assert outbox is None
    finally:
        await _delete_tenant_data(factory, tenant_id)


@pytest.mark.asyncio
async def test_committed_project_has_default_branch_and_outbox_event(
    database_engine: AsyncEngine,
) -> None:
    tenant_id = uuid.uuid4()
    actor_id = "oidc|commit-user"
    request_id = f"commit-{uuid.uuid4()}"
    factory = async_sessionmaker(database_engine, expire_on_commit=False)
    await _insert_tenant(factory, tenant_id)

    try:
        async with factory() as session, session.begin():
            service = ProjectService(
                SqlAlchemyProjectRepository(session), SqlAlchemyOutboxRepository(session)
            )
            created = await service.create(
                tenant_id,
                actor_id,
                CreateProject(request_id=request_id, title="长夜", language="zh-CN"),
            )
            project_id = created.project.id

        async with factory() as session, session.begin():
            existing_branch = await session.scalar(
                select(StoryBranch).where(
                    StoryBranch.tenant_id == tenant_id,
                    StoryBranch.project_id == project_id,
                    StoryBranch.name == "main",
                )
            )
            assert existing_branch is not None
            existing_branch.is_default = False

        async with factory() as session, session.begin():
            service = ProjectService(
                SqlAlchemyProjectRepository(session), SqlAlchemyOutboxRepository(session)
            )
            retried = await service.create(
                tenant_id,
                actor_id,
                CreateProject(request_id=request_id, title="长夜", language="zh-CN"),
            )
            assert retried.default_branch.id == existing_branch.id
            assert retried.default_branch.is_default is True

        async with factory() as session:
            project = await session.scalar(
                select(Project).where(Project.tenant_id == tenant_id, Project.id == project_id)
            )
            branch = await session.scalar(
                select(StoryBranch).where(
                    StoryBranch.tenant_id == tenant_id,
                    StoryBranch.project_id == project_id,
                    StoryBranch.name == "main",
                )
            )
            outbox_events = (
                await session.scalars(
                    select(OutboxEvent).where(
                        OutboxEvent.tenant_id == tenant_id,
                        OutboxEvent.aggregate_type == "project",
                        OutboxEvent.aggregate_id == project_id,
                    )
                )
            ).all()

        assert project is not None
        assert branch is not None
        assert branch.is_default is True
        assert len(outbox_events) == 1
        outbox = outbox_events[0]
        assert outbox.project_id == project_id
        assert outbox.event_type == "project.created.v1"
        assert outbox.payload["event_id"] == str(outbox.id)
        assert outbox.payload["tenant_id"] == str(tenant_id)
        assert outbox.payload["project_id"] == str(project_id)
        assert outbox.payload["branch_id"] == str(branch.id)
        assert outbox.payload["request_id"] == request_id
        assert outbox.payload["actor_id"] == actor_id
    finally:
        await _delete_tenant_data(factory, tenant_id)
