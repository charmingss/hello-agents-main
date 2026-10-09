"""In-process contracts backed by fakes; no Temporal server or PostgreSQL is used."""

from __future__ import annotations

import uuid
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from temporalio.client import WorkflowExecutionStatus, WorkflowQueryRejectedError
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner
from temporalio.workflow import _Definition

from novel_agent.api.dependencies import get_principal
from novel_agent.api.routes.workflows import get_project_workflow_client
from novel_agent.auth.models import Principal
from novel_agent.config import Settings
from novel_agent.main import create_app
from novel_agent.workflows.client import (
    STATUS_RPC_TIMEOUT,
    ProjectWorkflowClient,
    WorkflowNotFoundError,
    WorkflowServiceUnavailableError,
    normalize_workflow_status,
)
from novel_agent.workflows.project_workflow import (
    ACTIVITY_MAXIMUM_ATTEMPTS,
    ACTIVITY_START_TO_CLOSE_TIMEOUT,
    InitializeProjectInput,
    InitializeProjectOutput,
    InitializeProjectWorkflow,
)
from novel_agent.workflows.worker import ProjectActivities, serve_worker_until_stopped

TENANT_ID = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
OTHER_TENANT_ID = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
PROJECT_ID = uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
BRANCH_ID = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
USER_ID = "oidc|subject-without-uuid-shape"


def _workflow_input(request_id: str = "req-1") -> InitializeProjectInput:
    return InitializeProjectInput(
        request_id=request_id,
        tenant_id=TENANT_ID,
        actor_id=USER_ID,
        title="长夜",
        language="zh-CN",
    )


def test_workflow_contracts_are_frozen() -> None:
    command = _workflow_input()
    result = InitializeProjectOutput(project_id=PROJECT_ID, branch_id=BRANCH_ID)

    with pytest.raises(FrozenInstanceError):
        command.title = "篡改"  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        result.project_id = uuid.uuid4()  # type: ignore[misc]


@pytest.mark.asyncio
async def test_workflow_definition_loads_in_temporal_sandbox() -> None:
    definition = _Definition.must_from_class(InitializeProjectWorkflow)

    SandboxedWorkflowRunner().prepare_workflow(definition)


@pytest.mark.asyncio
async def test_workflow_only_schedules_bounded_activity(monkeypatch: pytest.MonkeyPatch) -> None:
    execute_activity = AsyncMock(
        return_value=InitializeProjectOutput(project_id=PROJECT_ID, branch_id=BRANCH_ID)
    )
    monkeypatch.setattr(
        "novel_agent.workflows.project_workflow.workflow.execute_activity",
        execute_activity,
    )

    result = await InitializeProjectWorkflow().run(_workflow_input())

    assert result == InitializeProjectOutput(project_id=PROJECT_ID, branch_id=BRANCH_ID)
    _, activity_input = execute_activity.call_args.args
    assert activity_input.request_id == "req-1"
    assert execute_activity.call_args.kwargs["start_to_close_timeout"] == (
        ACTIVITY_START_TO_CLOSE_TIMEOUT
    )
    assert (
        execute_activity.call_args.kwargs["retry_policy"].maximum_attempts
        == ACTIVITY_MAXIMUM_ATTEMPTS
    )


class _Session:
    def __init__(self) -> None:
        self.commits = 0
        self.rollbacks = 0

    async def __aenter__(self) -> _Session:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


class _SessionFactory:
    def __init__(self, session: _Session) -> None:
        self.session = session

    def __call__(self) -> _Session:
        return self.session


class _ActivityProjects:
    def __init__(self, existing: object | None = None) -> None:
        self.existing = existing
        self.request_ids: list[tuple[uuid.UUID, str]] = []

    async def get_by_request_id(self, tenant_id: uuid.UUID, request_id: str) -> object | None:
        self.request_ids.append((tenant_id, request_id))
        return self.existing


class _ActivityService:
    def __init__(self, created: object) -> None:
        self.created = created
        self.calls: list[tuple[uuid.UUID, str, object]] = []

    async def create(self, tenant_id: uuid.UUID, actor_id: str, request: object) -> object:
        self.calls.append((tenant_id, actor_id, request))
        return self.created


class _FailingActivityService(_ActivityService):
    async def create(self, tenant_id: uuid.UUID, actor_id: str, request: object) -> object:
        del tenant_id, actor_id, request
        raise RuntimeError("transient database failure")


@pytest.mark.asyncio
async def test_activity_creates_and_commits_exactly_once() -> None:
    session = _Session()
    created = SimpleNamespace(
        project=SimpleNamespace(id=PROJECT_ID),
        default_branch=SimpleNamespace(id=BRANCH_ID),
    )
    projects = _ActivityProjects()
    service = _ActivityService(created)
    activities = ProjectActivities(
        _SessionFactory(session),
        project_repository_factory=lambda _: projects,
        service_factory=lambda _projects, _outbox: service,
    )

    result = await activities.create_project_activity(_workflow_input())

    assert result == InitializeProjectOutput(project_id=PROJECT_ID, branch_id=BRANCH_ID)
    assert projects.request_ids == [(TENANT_ID, "req-1")]
    assert service.calls[0][0:2] == (TENANT_ID, USER_ID)
    assert session.commits == 1
    assert session.rollbacks == 0


@pytest.mark.asyncio
async def test_activity_retry_returns_existing_result_without_writing() -> None:
    session = _Session()
    existing = SimpleNamespace(
        project=SimpleNamespace(id=PROJECT_ID),
        default_branch=SimpleNamespace(id=BRANCH_ID),
    )
    projects = _ActivityProjects(existing)
    service = _ActivityService(existing)
    activities = ProjectActivities(
        _SessionFactory(session),
        project_repository_factory=lambda _: projects,
        service_factory=lambda _projects, _outbox: service,
    )

    first = await activities.create_project_activity(_workflow_input())
    second = await activities.create_project_activity(_workflow_input())

    assert first == second == InitializeProjectOutput(project_id=PROJECT_ID, branch_id=BRANCH_ID)
    assert service.calls == []
    assert session.commits == 0


@pytest.mark.asyncio
async def test_activity_rolls_back_without_committing_on_failure() -> None:
    session = _Session()
    projects = _ActivityProjects()
    activities = ProjectActivities(
        _SessionFactory(session),
        project_repository_factory=lambda _: projects,
        service_factory=lambda _projects, _outbox: _FailingActivityService(object()),
    )

    with pytest.raises(RuntimeError, match="transient database failure"):
        await activities.create_project_activity(_workflow_input())

    assert session.commits == 0
    assert session.rollbacks == 1


class _Handle:
    def __init__(
        self,
        workflow_id: str,
        status: WorkflowExecutionStatus,
        *,
        query_result: str = "waiting",
        query_error: Exception | None = None,
        descriptions: list[WorkflowExecutionStatus] | None = None,
    ) -> None:
        self.id = workflow_id
        self.status = status
        self.query_result = query_result
        self.query_error = query_error
        self.descriptions = descriptions or [status]
        self.queries = 0
        self.describe_timeouts: list[object] = []
        self.query_timeouts: list[object] = []

    async def describe(self, **kwargs: object) -> object:
        self.describe_timeouts.append(kwargs.get("rpc_timeout"))
        current = self.descriptions.pop(0) if len(self.descriptions) > 1 else self.descriptions[0]
        return SimpleNamespace(status=current)

    async def query(self, _: object, **kwargs: object) -> str:
        self.queries += 1
        self.query_timeouts.append(kwargs.get("rpc_timeout"))
        if self.query_error is not None:
            raise self.query_error
        return self.query_result


class _FakeTemporalClient:
    def __init__(self, handle: _Handle, *, duplicate_on_second_start: bool = False) -> None:
        self.handle = handle
        self.duplicate_on_second_start = duplicate_on_second_start
        self.starts: list[tuple[object, object, dict[str, object]]] = []
        self.get_handle_calls = 0

    async def start_workflow(self, workflow: object, command: object, **kwargs: object) -> _Handle:
        self.starts.append((workflow, command, kwargs))
        if self.duplicate_on_second_start and len(self.starts) == 2:
            raise WorkflowAlreadyStartedError(
                self.handle.id, "InitializeProjectWorkflow"
            )
        return self.handle

    def get_workflow_handle(self, workflow_id: str, **_: object) -> _Handle:
        self.get_handle_calls += 1
        assert workflow_id == self.handle.id
        return self.handle


@pytest.mark.asyncio
async def test_fake_client_contract_reuses_tenant_scoped_workflow_id() -> None:
    expected_id = f"project-init:{TENANT_ID}:req-1"
    handle = _Handle(expected_id, WorkflowExecutionStatus.RUNNING)
    temporal = _FakeTemporalClient(handle, duplicate_on_second_start=True)
    client = ProjectWorkflowClient(temporal, task_queue="project-init")

    first = await client.start(_workflow_input())
    second = await client.start(_workflow_input())

    assert first == second == expected_id
    assert [call[2]["id"] for call in temporal.starts] == [expected_id, expected_id]
    assert all(
        call[2]["id_conflict_policy"] is WorkflowIDConflictPolicy.USE_EXISTING
        for call in temporal.starts
    )
    assert all(
        call[2]["id_reuse_policy"] is WorkflowIDReusePolicy.REJECT_DUPLICATE
        for call in temporal.starts
    )
    assert temporal.get_handle_calls == 0


@pytest.mark.asyncio
async def test_fake_status_contract_bounds_describe_and_query_calls() -> None:
    workflow_id = f"project-init:{TENANT_ID}:req-1"
    handle = _Handle(workflow_id, WorkflowExecutionStatus.RUNNING)
    client = ProjectWorkflowClient(_FakeTemporalClient(handle), task_queue="project-init")

    assert await client.status(workflow_id) == "waiting"
    assert handle.describe_timeouts == [STATUS_RPC_TIMEOUT]
    assert handle.query_timeouts == [STATUS_RPC_TIMEOUT]


@pytest.mark.asyncio
async def test_fake_status_contract_preserves_completed_query_race_result() -> None:
    workflow_id = f"project-init:{TENANT_ID}:req-1"
    handle = _Handle(
        workflow_id,
        WorkflowExecutionStatus.RUNNING,
        query_result="completed",
    )
    client = ProjectWorkflowClient(_FakeTemporalClient(handle), task_queue="project-init")

    assert await client.status(workflow_id) == "completed"


@pytest.mark.asyncio
async def test_fake_status_contract_redescribes_after_query_completion_race() -> None:
    workflow_id = f"project-init:{TENANT_ID}:req-1"
    handle = _Handle(
        workflow_id,
        WorkflowExecutionStatus.RUNNING,
        query_error=WorkflowQueryRejectedError(WorkflowExecutionStatus.COMPLETED),
        descriptions=[WorkflowExecutionStatus.RUNNING, WorkflowExecutionStatus.COMPLETED],
    )
    client = ProjectWorkflowClient(_FakeTemporalClient(handle), task_queue="project-init")

    assert await client.status(workflow_id) == "completed"
    assert handle.describe_timeouts == [STATUS_RPC_TIMEOUT, STATUS_RPC_TIMEOUT]


@pytest.mark.asyncio
async def test_fake_status_contract_returns_safe_running_after_query_error() -> None:
    workflow_id = f"project-init:{TENANT_ID}:req-1"
    handle = _Handle(
        workflow_id,
        WorkflowExecutionStatus.RUNNING,
        query_error=WorkflowQueryRejectedError(WorkflowExecutionStatus.RUNNING),
    )
    client = ProjectWorkflowClient(_FakeTemporalClient(handle), task_queue="project-init")

    assert await client.status(workflow_id) == "running"
    assert handle.describe_timeouts == [STATUS_RPC_TIMEOUT, STATUS_RPC_TIMEOUT]


@pytest.mark.asyncio
async def test_fake_status_contract_classifies_temporal_rpc_failures() -> None:
    workflow_id = f"project-init:{TENANT_ID}:req-1"
    handle = _Handle(workflow_id, WorkflowExecutionStatus.RUNNING)
    temporal = _FakeTemporalClient(handle)
    client = ProjectWorkflowClient(temporal, task_queue="project-init")
    handle.describe = AsyncMock(
        side_effect=RPCError("missing", RPCStatusCode.NOT_FOUND, b"")
    )
    with pytest.raises(WorkflowNotFoundError):
        await client.status(workflow_id)

    handle.describe = AsyncMock(
        side_effect=RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")
    )
    with pytest.raises(WorkflowServiceUnavailableError):
        await client.status(workflow_id)

    handle.describe = AsyncMock(
        side_effect=RPCError("deadline", RPCStatusCode.DEADLINE_EXCEEDED, b"")
    )
    with pytest.raises(WorkflowServiceUnavailableError):
        await client.status(workflow_id)


@pytest.mark.parametrize(
    ("temporal_status", "expected"),
    [
        (None, "pending"),
        (WorkflowExecutionStatus.RUNNING, "running"),
        (WorkflowExecutionStatus.COMPLETED, "completed"),
        (WorkflowExecutionStatus.FAILED, "failed"),
        (WorkflowExecutionStatus.TIMED_OUT, "failed"),
        (WorkflowExecutionStatus.TERMINATED, "failed"),
        (WorkflowExecutionStatus.CANCELED, "canceled"),
    ],
)
def test_temporal_statuses_are_normalized(
    temporal_status: WorkflowExecutionStatus | None, expected: str
) -> None:
    assert normalize_workflow_status(temporal_status) == expected


class _FakeWorkflowGateway:
    def __init__(self, *, error: Exception | None = None) -> None:
        self.commands: list[InitializeProjectInput] = []
        self.describe_ids: list[str] = []
        self.error = error

    async def start(self, command: InitializeProjectInput) -> str:
        self.commands.append(command)
        return f"project-init:{command.tenant_id}:{command.request_id}"

    async def status(self, workflow_id: str) -> str:
        self.describe_ids.append(workflow_id)
        if self.error is not None:
            raise self.error
        return "waiting"


def _api_client(gateway: _FakeWorkflowGateway, tenant_id: uuid.UUID = TENANT_ID) -> TestClient:
    app = create_app(Settings(environment="test"))
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject=USER_ID, tenant_id=tenant_id
    )
    app.dependency_overrides[get_project_workflow_client] = lambda: gateway
    return TestClient(app)


def test_start_workflow_returns_202_and_uses_only_principal_identity() -> None:
    gateway = _FakeWorkflowGateway()
    with _api_client(gateway) as client:
        response = client.post(
            "/api/v1/workflows/project-initializations",
            json={"request_id": "req-1", "title": "长夜", "language": "zh-CN"},
        )

    assert response.status_code == 202
    assert response.json() == {"workflow_id": f"project-init:{TENANT_ID}:req-1"}
    assert gateway.commands == [_workflow_input()]


def test_fake_api_contract_returns_same_id_when_second_post_is_already_started() -> None:
    workflow_id = f"project-init:{TENANT_ID}:req-1"
    temporal = _FakeTemporalClient(
        _Handle(workflow_id, WorkflowExecutionStatus.RUNNING),
        duplicate_on_second_start=True,
    )
    gateway = ProjectWorkflowClient(temporal, task_queue="project-init")
    app = create_app(Settings(environment="test"))
    app.dependency_overrides[get_principal] = lambda: Principal(
        subject=USER_ID, tenant_id=TENANT_ID
    )
    app.dependency_overrides[get_project_workflow_client] = lambda: gateway

    with TestClient(app) as client:
        first = client.post(
            "/api/v1/workflows/project-initializations",
            json={"request_id": "req-1", "title": "长夜"},
        )
        second = client.post(
            "/api/v1/workflows/project-initializations",
            json={"request_id": "req-1", "title": "长夜"},
        )

    assert first.status_code == second.status_code == 202
    assert first.json() == second.json() == {"workflow_id": workflow_id}


def test_start_workflow_rejects_client_supplied_identity() -> None:
    gateway = _FakeWorkflowGateway()
    with _api_client(gateway) as client:
        response = client.post(
            "/api/v1/workflows/project-initializations",
            json={
                "request_id": "req-1",
                "title": "长夜",
                "tenant_id": str(OTHER_TENANT_ID),
                "actor_id": "attacker",
            },
        )

    assert response.status_code == 422
    assert gateway.commands == []


@pytest.mark.parametrize("request_id", ["path/escape", "ambiguous:segment", " space"])
def test_start_workflow_rejects_request_ids_unsafe_for_workflow_paths(request_id: str) -> None:
    gateway = _FakeWorkflowGateway()
    with _api_client(gateway) as client:
        response = client.post(
            "/api/v1/workflows/project-initializations",
            json={"request_id": request_id, "title": "长夜"},
        )

    assert response.status_code == 422
    assert gateway.commands == []


def test_get_workflow_returns_normalized_status_for_principal_tenant() -> None:
    gateway = _FakeWorkflowGateway()
    workflow_id = f"project-init:{TENANT_ID}:req-1"
    with _api_client(gateway) as client:
        response = client.get(f"/api/v1/workflows/{workflow_id}")

    assert response.status_code == 200
    assert response.json() == {"workflow_id": workflow_id, "status": "waiting"}
    assert gateway.describe_ids == [workflow_id]


def test_get_workflow_hides_another_tenants_workflow() -> None:
    gateway = _FakeWorkflowGateway()
    workflow_id = f"project-init:{OTHER_TENANT_ID}:req-1"
    with _api_client(gateway) as client:
        response = client.get(f"/api/v1/workflows/{workflow_id}")

    assert response.status_code == 404
    assert gateway.describe_ids == []


@pytest.mark.parametrize(
    ("error", "expected_status", "problem_type"),
    [
        (WorkflowNotFoundError(), 404, "workflow-not-found"),
        (WorkflowServiceUnavailableError(), 503, "workflow-service-unavailable"),
    ],
)
def test_fake_api_contract_maps_temporal_failures_to_problem_details(
    error: Exception, expected_status: int, problem_type: str
) -> None:
    gateway = _FakeWorkflowGateway(error=error)
    workflow_id = f"project-init:{TENANT_ID}:req-1"
    with _api_client(gateway) as client:
        response = client.get(f"/api/v1/workflows/{workflow_id}")

    assert response.status_code == expected_status
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"].endswith(problem_type)


class _FakeWorkerContext:
    def __init__(self) -> None:
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> _FakeWorkerContext:
        self.entered = True
        return self

    async def __aexit__(self, *_: object) -> None:
        self.exited = True


@pytest.mark.asyncio
async def test_fake_worker_lifecycle_exits_context_after_stop_request() -> None:
    worker = _FakeWorkerContext()
    stop = __import__("asyncio").Event()
    stop.set()

    await serve_worker_until_stopped(worker, stop)

    assert worker.entered is True
    assert worker.exited is True
