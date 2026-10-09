from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, Protocol, cast

from temporalio.client import (
    Client,
    WorkflowExecutionStatus,
    WorkflowQueryFailedError,
    WorkflowQueryRejectedError,
)
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.service import RPCError, RPCStatusCode

from novel_agent.config import Settings
from novel_agent.workflows.project_workflow import (
    InitializeProjectInput,
    InitializeProjectOutput,
    InitializeProjectWorkflow,
)
from novel_agent.workflows.source_ingestion import (
    SourceIngestionInput,
    SourceIngestionOutput,
    SourceIngestionStatus,
    SourceIngestionWorkflow,
    source_ingestion_workflow_id,
)

NormalizedWorkflowStatus = Literal[
    "pending", "running", "waiting", "completed", "failed", "canceled"
]
STATUS_RPC_TIMEOUT = timedelta(seconds=3)
SOURCE_INGESTION_FINGERPRINT_MEMO_KEY = "source_ingestion_fingerprint"


class WorkflowNotFoundError(Exception):
    pass


class WorkflowServiceUnavailableError(Exception):
    pass


class WorkflowIdentityConflictError(Exception):
    pass


@dataclass(frozen=True)
class WorkflowStatusSnapshot:
    status: NormalizedWorkflowStatus
    stage: str | None = None
    progress: int | None = None
    error_code: str | None = None


class WorkflowClientProtocol(Protocol):
    async def start_workflow(self, workflow: object, arg: object, **kwargs: object) -> object: ...

    def get_workflow_handle(self, workflow_id: str, **kwargs: object) -> object: ...


def project_initialization_workflow_id(command: InitializeProjectInput) -> str:
    return f"project-init:{command.tenant_id}:{command.request_id}"


def normalize_workflow_status(
    status: WorkflowExecutionStatus | None,
) -> NormalizedWorkflowStatus:
    if status is None:
        return "pending"
    if status is WorkflowExecutionStatus.RUNNING:
        return "running"
    if status is WorkflowExecutionStatus.COMPLETED:
        return "completed"
    if status is WorkflowExecutionStatus.CANCELED:
        return "canceled"
    return "failed"


def _raise_status_rpc_error(error: RPCError) -> None:
    if error.status is RPCStatusCode.NOT_FOUND:
        raise WorkflowNotFoundError from error
    raise WorkflowServiceUnavailableError from error


class ProjectWorkflowClient:
    def __init__(self, client: WorkflowClientProtocol, *, task_queue: str) -> None:
        self._client = client
        self._task_queue = task_queue

    async def start(self, command: InitializeProjectInput) -> str:
        workflow_id = project_initialization_workflow_id(command)
        try:
            await self._client.start_workflow(
                InitializeProjectWorkflow.run,
                command,
                id=workflow_id,
                task_queue=self._task_queue,
                result_type=InitializeProjectOutput,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            )
        except WorkflowAlreadyStartedError:
            # The stable ID is the public handle for both active and completed retries.
            pass
        except RPCError as exc:
            raise WorkflowServiceUnavailableError from exc
        return workflow_id

    async def status(self, workflow_id: str) -> NormalizedWorkflowStatus:
        handle = self._client.get_workflow_handle(workflow_id, result_type=InitializeProjectOutput)
        try:
            description = await handle.describe(  # type: ignore[attr-defined]
                rpc_timeout=STATUS_RPC_TIMEOUT
            )
        except RPCError as exc:
            _raise_status_rpc_error(exc)
        normalized = normalize_workflow_status(description.status)
        if normalized != "running":
            return normalized
        try:
            queried = await handle.query(  # type: ignore[attr-defined]
                InitializeProjectWorkflow.state,
                rpc_timeout=STATUS_RPC_TIMEOUT,
            )
        except (WorkflowQueryFailedError, WorkflowQueryRejectedError, RPCError):
            try:
                description = await handle.describe(  # type: ignore[attr-defined]
                    rpc_timeout=STATUS_RPC_TIMEOUT
                )
            except RPCError as exc:
                _raise_status_rpc_error(exc)
            refreshed = normalize_workflow_status(description.status)
            return "running" if refreshed == "running" else refreshed
        if queried in {"pending", "running", "waiting", "completed", "failed", "canceled"}:
            return cast(NormalizedWorkflowStatus, queried)
        return "running"


class SourceIngestionWorkflowClient:
    def __init__(self, client: WorkflowClientProtocol, *, task_queue: str) -> None:
        self._client = client
        self._task_queue = task_queue

    async def start(
        self,
        command: SourceIngestionInput,
        *,
        workflow_id: str | None = None,
    ) -> str:
        expected_id = source_ingestion_workflow_id(command)
        if workflow_id is not None and workflow_id != expected_id:
            raise ValueError("workflow id does not match source ingestion identity")
        workflow_id = expected_id
        handle = self._client.get_workflow_handle(workflow_id, result_type=SourceIngestionOutput)
        try:
            description = await handle.describe(  # type: ignore[attr-defined]
                rpc_timeout=STATUS_RPC_TIMEOUT
            )
        except RPCError as exc:
            if exc.status is not RPCStatusCode.NOT_FOUND:
                raise WorkflowServiceUnavailableError from exc
        else:
            self._verify_fingerprint_memo(description, command)
            return workflow_id

        try:
            await self._client.start_workflow(
                SourceIngestionWorkflow.run,
                command,
                id=workflow_id,
                task_queue=self._task_queue,
                result_type=SourceIngestionOutput,
                id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
                id_conflict_policy=WorkflowIDConflictPolicy.FAIL,
                memo={SOURCE_INGESTION_FINGERPRINT_MEMO_KEY: command.fingerprint()},
            )
        except WorkflowAlreadyStartedError:
            try:
                description = await handle.describe(  # type: ignore[attr-defined]
                    rpc_timeout=STATUS_RPC_TIMEOUT
                )
            except RPCError as exc:
                raise WorkflowServiceUnavailableError from exc
            self._verify_fingerprint_memo(description, command)
        except RPCError as exc:
            raise WorkflowServiceUnavailableError from exc
        return workflow_id

    @staticmethod
    def _verify_fingerprint_memo(description: object, command: SourceIngestionInput) -> None:
        memo_reader = getattr(description, "memo", None)
        if not callable(memo_reader):
            raise WorkflowIdentityConflictError
        try:
            memo = memo_reader()
        except Exception as exc:
            raise WorkflowIdentityConflictError from exc
        if (
            not isinstance(memo, dict)
            or memo.get(SOURCE_INGESTION_FINGERPRINT_MEMO_KEY) != command.fingerprint()
        ):
            raise WorkflowIdentityConflictError

    async def status(self, workflow_id: str) -> WorkflowStatusSnapshot:
        handle = self._client.get_workflow_handle(workflow_id, result_type=SourceIngestionOutput)
        try:
            description = await handle.describe(rpc_timeout=STATUS_RPC_TIMEOUT)  # type: ignore[attr-defined]
        except RPCError as exc:
            _raise_status_rpc_error(exc)
        normalized = normalize_workflow_status(description.status)
        if normalized == "completed":
            return WorkflowStatusSnapshot("completed", "completed", 100)
        if normalized == "failed":
            return WorkflowStatusSnapshot("failed", "failed", 100, "ingestion_failed")
        if normalized == "canceled":
            return WorkflowStatusSnapshot("canceled", "canceled", 100, "ingestion_canceled")
        try:
            detail = await handle.query(  # type: ignore[attr-defined]
                SourceIngestionWorkflow.status,
                rpc_timeout=STATUS_RPC_TIMEOUT,
            )
        except (WorkflowQueryFailedError, WorkflowQueryRejectedError, RPCError):
            try:
                description = await handle.describe(  # type: ignore[attr-defined]
                    rpc_timeout=STATUS_RPC_TIMEOUT
                )
            except RPCError as exc:
                _raise_status_rpc_error(exc)
            normalized = normalize_workflow_status(description.status)
            if normalized == "completed":
                return WorkflowStatusSnapshot("completed", "completed", 100)
            if normalized == "failed":
                return WorkflowStatusSnapshot("failed", "failed", 100, "ingestion_failed")
            if normalized == "canceled":
                return WorkflowStatusSnapshot("canceled", "canceled", 100, "ingestion_canceled")
            return WorkflowStatusSnapshot(normalized)
        if not isinstance(detail, SourceIngestionStatus):
            return WorkflowStatusSnapshot("running")
        return WorkflowStatusSnapshot(
            detail.state, detail.stage, detail.progress, detail.error_code
        )


class TemporalClientProvider:
    """Creates one Temporal client on first workflow request and reuses it."""

    def __init__(self, settings: Settings, client: Client | None = None) -> None:
        self._settings = settings
        self._client = client
        self._lock = asyncio.Lock()

    async def get(self) -> Client:
        if self._client is not None:
            return self._client
        async with self._lock:
            if self._client is None:
                try:
                    self._client = await Client.connect(
                        self._settings.temporal_target,
                        namespace=self._settings.temporal_namespace,
                    )
                except (RPCError, ConnectionError, TimeoutError) as exc:
                    raise WorkflowServiceUnavailableError from exc
        return self._client


class ProviderSourceIngestionWorkflowGateway:
    """Lazy API adapter: constructing the app never opens a Temporal connection."""

    def __init__(self, provider: TemporalClientProvider, *, task_queue: str) -> None:
        self._provider = provider
        self._task_queue = task_queue

    async def start(self, *, workflow_id: str, command: SourceIngestionInput) -> str:
        client = SourceIngestionWorkflowClient(
            cast(WorkflowClientProtocol, await self._provider.get()),
            task_queue=self._task_queue,
        )
        return await client.start(command, workflow_id=workflow_id)

    async def status(self, workflow_id: str) -> WorkflowStatusSnapshot:
        client = SourceIngestionWorkflowClient(
            cast(WorkflowClientProtocol, await self._provider.get()),
            task_queue=self._task_queue,
        )
        return await client.status(workflow_id)
