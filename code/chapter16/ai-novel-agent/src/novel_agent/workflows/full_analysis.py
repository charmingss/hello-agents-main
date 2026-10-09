"""Deterministic Temporal orchestration for full-source analysis jobs.

The workflow owns no SQLAlchemy, FastAPI, provider or wall-clock state. It only
asks Activities for the next pending batch, analyzes it with bounded retries,
and calls one finalize activity after the last batch succeeds.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TYPE_CHECKING, Literal, Protocol, cast

from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError

from novel_agent.analysis.contracts import AnalysisError
from novel_agent.vector.schema import TenantProjectScope

if TYPE_CHECKING:
    from novel_agent.analysis.jobs import BatchClaim

NEXT_ACTIVITY_TIMEOUT = timedelta(seconds=30)
ANALYZE_ACTIVITY_TIMEOUT = timedelta(minutes=10)
FINALIZE_ACTIVITY_TIMEOUT = timedelta(seconds=30)
FULL_ANALYSIS_MAXIMUM_ATTEMPTS = 4


def full_analysis_workflow_id(job_id: uuid.UUID) -> str:
    return f"full-analysis:{job_id}"


@dataclass(frozen=True)
class FullAnalysisInput:
    tenant_id: uuid.UUID
    project_id: uuid.UUID
    source_id: uuid.UUID
    job_id: uuid.UUID


@dataclass(frozen=True)
class FullAnalysisBatchInput:
    """Adds the resolved batch index to the job identity for analyze calls."""

    tenant_id: uuid.UUID
    project_id: uuid.UUID
    source_id: uuid.UUID
    job_id: uuid.UUID
    batch_index: int


@dataclass(frozen=True)
class FullAnalysisOutput:
    analysis_id: uuid.UUID


@dataclass(frozen=True)
class FullAnalysisStatus:
    state: Literal["pending", "running", "succeeded", "failed"] = "pending"
    progress_batches: int = 0
    error_code: str | None = None

    def __post_init__(self) -> None:
        if self.state not in {"pending", "running", "succeeded", "failed"}:
            raise ValueError("state must be one of the allowed literals")
        if self.progress_batches < 0:
            raise ValueError("progress_batches must be non-negative")
        if self.error_code is not None and (
            len(self.error_code) > 64
            or not self.error_code.replace("_", "a").isalnum()
            or not self.error_code[0].isalpha()
        ):
            raise ValueError("error_code must be a stable token")


def _scope(command: FullAnalysisInput | FullAnalysisBatchInput) -> TenantProjectScope:
    return TenantProjectScope(
        tenant_id=command.tenant_id, project_id=command.project_id
    )


class FullAnalysisServicePort(Protocol):
    """The subset of ``FullAnalysisJobService`` used by the activities."""

    async def next_batch_index(
        self, scope: TenantProjectScope, source_id: uuid.UUID, job_id: uuid.UUID
    ) -> int | None: ...

    async def claim_batch(
        self,
        scope: TenantProjectScope,
        source_id: uuid.UUID,
        job_id: uuid.UUID,
        *,
        batch_index: int,
    ) -> BatchClaim: ...

    async def analyze_claim(self, claim: BatchClaim) -> dict[str, object]: ...

    async def complete_batch(
        self, claim: BatchClaim, candidate: dict[str, object]
    ) -> bool: ...

    async def fail_batch(self, claim: BatchClaim, error_code: str) -> bool: ...

    async def finalize(
        self, scope: TenantProjectScope, source_id: uuid.UUID, job_id: uuid.UUID
    ) -> uuid.UUID: ...


def _raise_analysis_error(exc: AnalysisError) -> None:
    raise ApplicationError(
        "full analysis operation rejected",
        type=exc.code,
        non_retryable=True,
    ) from None


def _noop_heartbeat(_stage: object) -> None:
    pass


def _not_cancelled() -> bool:
    return False


class FullAnalysisActivities:
    """Activity adapters that delegate to the SQL-backed job service."""

    def __init__(
        self,
        service: FullAnalysisServicePort,
        *,
        heartbeat: Callable[[object], None] | None = None,
        cancellation_probe: Callable[[], bool] | None = None,
    ) -> None:
        self._service = service
        self._heartbeat: Callable[[object], None] = heartbeat or _noop_heartbeat
        self._cancellation_probe: Callable[[], bool] = (
            cancellation_probe or _not_cancelled
        )

    @activity.defn(name="full_analysis_next_batch_activity")
    async def next_batch_index(self, command: FullAnalysisInput) -> int | None:
        self._heartbeat("next")
        if self._cancellation_probe():
            raise asyncio.CancelledError
        scope = _scope(command)
        try:
            return await self._service.next_batch_index(
                scope, command.source_id, command.job_id
            )
        except AnalysisError as exc:
            _raise_analysis_error(exc)
            raise  # unreachable; satisfies mypy return type

    @activity.defn(name="full_analysis_analyze_batch_activity")
    async def analyze_batch(self, command: FullAnalysisBatchInput) -> None:
        service = self._service
        scope = _scope(command)
        self._heartbeat("claim")
        if self._cancellation_probe():
            raise asyncio.CancelledError
        try:
            claim = await service.claim_batch(
                scope,
                command.source_id,
                command.job_id,
                batch_index=command.batch_index,
            )
        except AnalysisError as exc:
            _raise_analysis_error(exc)
            raise  # unreachable
        self._heartbeat("analyze")
        try:
            candidate = await service.analyze_claim(claim)
        except AnalysisError as exc:
            await service.fail_batch(claim, exc.code)
            _raise_analysis_error(exc)
            raise  # unreachable
        self._heartbeat("complete")
        if not await service.complete_batch(claim, candidate):
            await service.fail_batch(claim, "analysis_job_conflict")

    @activity.defn(name="full_analysis_finalize_activity")
    async def finalize(self, command: FullAnalysisInput) -> uuid.UUID:
        self._heartbeat("finalize")
        if self._cancellation_probe():
            raise asyncio.CancelledError
        scope = _scope(command)
        try:
            return await self._service.finalize(
                scope, command.source_id, command.job_id
            )
        except AnalysisError as exc:
            _raise_analysis_error(exc)
            raise  # unreachable


@workflow.defn
class FullAnalysisWorkflow:
    def __init__(self) -> None:
        self._status = FullAnalysisStatus()

    @workflow.query
    def status(self) -> FullAnalysisStatus:
        return self._status

    @workflow.run
    async def run(self, command: FullAnalysisInput) -> FullAnalysisOutput:
        self._status = FullAnalysisStatus("running")
        try:
            while True:
                batch_index = cast(
                    int | None,
                    await workflow.execute_activity(
                        "full_analysis_next_batch_activity",
                        command,
                        start_to_close_timeout=NEXT_ACTIVITY_TIMEOUT,
                        retry_policy=RetryPolicy(
                            maximum_attempts=FULL_ANALYSIS_MAXIMUM_ATTEMPTS
                        ),
                    ),
                )
                if batch_index is None:
                    break
                batch_input = FullAnalysisBatchInput(
                    tenant_id=command.tenant_id,
                    project_id=command.project_id,
                    source_id=command.source_id,
                    job_id=command.job_id,
                    batch_index=batch_index,
                )
                await workflow.execute_activity(
                    "full_analysis_analyze_batch_activity",
                    batch_input,
                    start_to_close_timeout=ANALYZE_ACTIVITY_TIMEOUT,
                    retry_policy=RetryPolicy(
                        initial_interval=timedelta(seconds=1),
                        backoff_coefficient=2.0,
                        maximum_interval=timedelta(seconds=30),
                        maximum_attempts=FULL_ANALYSIS_MAXIMUM_ATTEMPTS,
                    ),
                )
                self._status = FullAnalysisStatus(
                    "running", progress_batches=batch_index + 1
                )
            analysis_id = await workflow.execute_activity(
                "full_analysis_finalize_activity",
                command,
                start_to_close_timeout=FINALIZE_ACTIVITY_TIMEOUT,
                retry_policy=RetryPolicy(
                    maximum_attempts=FULL_ANALYSIS_MAXIMUM_ATTEMPTS
                ),
            )
        except asyncio.CancelledError:
            self._status = FullAnalysisStatus(
                "failed", self._status.progress_batches, "analysis_canceled"
            )
            raise
        except BaseException as exc:
            code = self._public_error_code(exc)
            self._status = FullAnalysisStatus(
                "failed", self._status.progress_batches, code
            )
            raise
        self._status = FullAnalysisStatus(
            "succeeded", self._status.progress_batches
        )
        return FullAnalysisOutput(
            analysis_id=cast(uuid.UUID, analysis_id)
        )

    @staticmethod
    def _public_error_code(error: BaseException) -> str:
        current: BaseException | None = error
        for _ in range(8):
            if current is None:
                return "analysis_failed"
            value = getattr(current, "type", None)
            if isinstance(value, str) and value and value.replace("_", "a").isalnum():
                return value
            current = current.__cause__
        return "analysis_failed"


__all__ = [
    "ANALYZE_ACTIVITY_TIMEOUT",
    "FINALIZE_ACTIVITY_TIMEOUT",
    "FULL_ANALYSIS_MAXIMUM_ATTEMPTS",
    "FullAnalysisActivities",
    "FullAnalysisBatchInput",
    "FullAnalysisInput",
    "FullAnalysisOutput",
    "FullAnalysisStatus",
    "FullAnalysisWorkflow",
    "NEXT_ACTIVITY_TIMEOUT",
    "full_analysis_workflow_id",
]
