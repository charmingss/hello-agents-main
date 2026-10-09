"""Environment-backed smoke test for the complete platform-foundation path."""

from __future__ import annotations

import asyncio
import os
import uuid

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from temporalio.client import Client
from temporalio.worker import Worker

from novel_agent.api.dependencies import get_principal
from novel_agent.auth.models import Principal
from novel_agent.config import Settings
from novel_agent.db.models.identity import Tenant
from novel_agent.db.models.outbox import OutboxEvent
from novel_agent.db.models.project import Project, StoryBranch
from novel_agent.db.session import create_engine, create_session_factory
from novel_agent.main import create_app
from novel_agent.workflows.project_workflow import (
    InitializeProjectOutput,
    InitializeProjectWorkflow,
)
from novel_agent.workflows.worker import ProjectActivities

RUN_EXTERNAL_TESTS = os.environ.get("RUN_EXTERNAL_TESTS") == "1"
REQUEST_ID = "smoke-1"

pytestmark = [
    pytest.mark.external,
    pytest.mark.skipif(
        not RUN_EXTERNAL_TESTS,
        reason="set RUN_EXTERNAL_TESTS=1 with PostgreSQL and Temporal available",
    ),
]


async def _ensure_tenant(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID, tenant_slug: str
) -> None:
    statement = (
        insert(Tenant)
        .values(id=tenant_id, slug=tenant_slug, name="Foundation Smoke")
        .on_conflict_do_nothing(index_elements=[Tenant.id])
    )
    async with factory.begin() as session:
        await session.execute(statement)


async def _cleanup_tenant(
    factory: async_sessionmaker[AsyncSession], tenant_id: uuid.UUID
) -> None:
    """Delete smoke-owned rows in foreign-key order within one transaction."""
    async with factory.begin() as session:
        await session.execute(delete(OutboxEvent).where(OutboxEvent.tenant_id == tenant_id))
        await session.execute(delete(StoryBranch).where(StoryBranch.tenant_id == tenant_id))
        await session.execute(delete(Project).where(Project.tenant_id == tenant_id))
        await session.execute(delete(Tenant).where(Tenant.id == tenant_id))


@pytest.mark.asyncio
async def test_foundation_workflow_is_tenant_safe_and_idempotent() -> None:
    tenant_id = uuid.uuid4()
    tenant_slug = f"foundation-smoke-{tenant_id.hex}"
    principal = Principal(subject="oidc|foundation-smoke", tenant_id=tenant_id)
    settings = Settings(
        environment="test",
        database_url=os.environ.get(
            "TEST_DATABASE_URL",
            "postgresql+asyncpg://novel:novel@localhost:5432/novel",
        ),
        temporal_target=os.environ.get("TEMPORAL_TARGET", "localhost:7233"),
        temporal_task_queue="novel-agent-foundation-smoke",
    )
    engine = create_engine(settings)
    factory = create_session_factory(engine)
    app = None
    primary_error: BaseException | None = None
    try:
        temporal = await asyncio.wait_for(
            Client.connect(
                settings.temporal_target,
                namespace=settings.temporal_namespace,
            ),
            timeout=10,
        )
        activities = ProjectActivities(factory)
        worker = Worker(
            temporal,
            task_queue=settings.temporal_task_queue,
            workflows=[InitializeProjectWorkflow],
            activities=[activities.create_project_activity],
        )
        app = create_app(settings)
        app.dependency_overrides[get_principal] = lambda: principal

        await _ensure_tenant(factory, tenant_id, tenant_slug)
        async with worker, app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test"
            ) as client:
                payload = {"request_id": REQUEST_ID, "title": "长夜", "language": "zh-CN"}
                first = await client.post("/api/v1/workflows/project-initializations", json=payload)
                assert first.status_code == 202
                workflow_id = first.json()["workflow_id"]

                handle = temporal.get_workflow_handle(
                    workflow_id, result_type=InitializeProjectOutput
                )
                result = await asyncio.wait_for(handle.result(), timeout=30)

                fetched = await client.get(f"/api/v1/projects/{result.project_id}")
                assert fetched.status_code == 200
                assert fetched.json()["default_branch"] == {
                    "id": str(result.branch_id),
                    "name": "main",
                    "is_default": True,
                }

                second = await client.post(
                    "/api/v1/workflows/project-initializations", json=payload
                )
                assert second.status_code == 202
                assert second.json() == first.json()
                repeated = await asyncio.wait_for(handle.result(), timeout=30)
                assert repeated == result

        async with factory() as session:
            project_count = await session.scalar(
                select(func.count())
                .select_from(Project)
                .where(
                    Project.tenant_id == tenant_id,
                    Project.creation_request_id == REQUEST_ID,
                )
            )
            branch_count = await session.scalar(
                select(func.count())
                .select_from(StoryBranch)
                .where(
                    StoryBranch.tenant_id == tenant_id,
                    StoryBranch.project_id == result.project_id,
                    StoryBranch.name == "main",
                    StoryBranch.is_default.is_(True),
                )
            )
            event_count = await session.scalar(
                select(func.count())
                .select_from(OutboxEvent)
                .where(
                    OutboxEvent.tenant_id == tenant_id,
                    OutboxEvent.project_id == result.project_id,
                    OutboxEvent.event_type == "project.created.v1",
                )
            )

        assert project_count == 1
        assert branch_count == 1
        assert event_count == 1
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if app is not None:
            app.dependency_overrides.clear()

        cleanup_errors: list[Exception] = []
        try:
            await _cleanup_tenant(factory, tenant_id)
        except Exception as exc:
            cleanup_errors.append(exc)
        try:
            await engine.dispose()
        except Exception as exc:
            cleanup_errors.append(exc)

        if cleanup_errors:
            cleanup_failure = ExceptionGroup("foundation smoke cleanup failed", cleanup_errors)
            if primary_error is None:
                raise cleanup_failure
            primary_error.add_note(f"Additional cleanup failure: {cleanup_failure!r}")
