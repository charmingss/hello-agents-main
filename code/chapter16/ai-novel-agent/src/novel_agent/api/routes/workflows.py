from typing import Annotated, Literal, Protocol

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, ConfigDict, Field

from novel_agent.api.dependencies import get_principal
from novel_agent.auth.models import Principal
from novel_agent.projects.schemas import ProjectTitle
from novel_agent.workflows.client import (
    NormalizedWorkflowStatus,
    ProjectWorkflowClient,
    WorkflowStatusSnapshot,
)
from novel_agent.workflows.project_workflow import InitializeProjectInput

router = APIRouter(prefix="/workflows", tags=["workflows"])


class ProjectWorkflowGateway(Protocol):
    async def start(self, command: InitializeProjectInput) -> str: ...

    async def status(self, workflow_id: str) -> NormalizedWorkflowStatus: ...


class StartWorkflowResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    workflow_id: str


class StartProjectInitialization(BaseModel):
    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    request_id: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]*$",
    )
    title: ProjectTitle
    language: Literal["zh-CN"] = "zh-CN"


class WorkflowStatusResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    workflow_id: str
    status: Literal["pending", "running", "waiting", "completed", "failed", "canceled"]
    stage: str | None = None
    progress: int | None = None
    error_code: str | None = None


async def get_project_workflow_client(request: Request) -> ProjectWorkflowClient:
    client = await request.app.state.temporal_client_provider.get()
    return ProjectWorkflowClient(client, task_queue=request.app.state.temporal_task_queue)


PrincipalDependency = Annotated[Principal, Depends(get_principal)]
WorkflowClientDependency = Annotated[ProjectWorkflowGateway, Depends(get_project_workflow_client)]


@router.post(
    "/project-initializations",
    response_model=StartWorkflowResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def start_project_initialization(
    command: StartProjectInitialization,
    principal: PrincipalDependency,
    client: WorkflowClientDependency,
) -> StartWorkflowResponse:
    workflow_id = await client.start(
        InitializeProjectInput(
            request_id=command.request_id,
            tenant_id=principal.tenant_id,
            actor_id=principal.subject,
            title=command.title,
            language=command.language,
        )
    )
    return StartWorkflowResponse(workflow_id=workflow_id)


@router.get(
    "/{workflow_id}",
    response_model=WorkflowStatusResponse,
    response_model_exclude_none=True,
)
async def get_workflow_status(
    workflow_id: str,
    request: Request,
    principal: PrincipalDependency,
    client: WorkflowClientDependency,
) -> WorkflowStatusResponse:
    project_prefix = f"project-init:{principal.tenant_id}:"
    source_prefix = f"source-ingestion:{principal.tenant_id}:"
    if not workflow_id.startswith((project_prefix, source_prefix)) or workflow_id in {
        project_prefix,
        source_prefix,
    }:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    if workflow_id.startswith(source_prefix):
        source_gateway = request.app.state.source_ingestion_workflow_gateway
        snapshot = await source_gateway.status(workflow_id)
        if not isinstance(snapshot, WorkflowStatusSnapshot):
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE)
        return WorkflowStatusResponse(
            workflow_id=workflow_id,
            status=snapshot.status,
            stage=snapshot.stage,
            progress=snapshot.progress,
            error_code=snapshot.error_code,
        )
    normalized = await client.status(workflow_id)
    return WorkflowStatusResponse(workflow_id=workflow_id, status=normalized)
