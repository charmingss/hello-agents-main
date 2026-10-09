from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, cast

from temporalio import workflow
from temporalio.common import RetryPolicy

ACTIVITY_START_TO_CLOSE_TIMEOUT = timedelta(seconds=30)
ACTIVITY_MAXIMUM_ATTEMPTS = 5


@dataclass(frozen=True)
class InitializeProjectInput:
    request_id: str
    tenant_id: uuid.UUID
    actor_id: str
    title: str
    language: Literal["zh-CN"] = "zh-CN"


@dataclass(frozen=True)
class InitializeProjectOutput:
    project_id: uuid.UUID
    branch_id: uuid.UUID


@workflow.defn
class InitializeProjectWorkflow:
    def __init__(self) -> None:
        self._state = "pending"

    @workflow.query
    def state(self) -> str:
        return self._state

    @workflow.run
    async def run(self, command: InitializeProjectInput) -> InitializeProjectOutput:
        self._state = "running"
        self._state = "waiting"
        result = cast(
            InitializeProjectOutput,
            await workflow.execute_activity(
                "create_project_activity",
                command,
                start_to_close_timeout=ACTIVITY_START_TO_CLOSE_TIMEOUT,
                retry_policy=RetryPolicy(maximum_attempts=ACTIVITY_MAXIMUM_ATTEMPTS),
                result_type=InitializeProjectOutput,
            ),
        )
        self._state = "completed"
        return result
