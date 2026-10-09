from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Protocol

from fastapi import APIRouter, Depends, Query, Request, status
from pydantic import BaseModel, ConfigDict

from novel_agent.api.dependencies import get_principal
from novel_agent.auth.models import Principal
from novel_agent.sources.api_service import (
    CompletedSource,
    InitiatedUpload,
    SourceApiService,
    SourcePage,
    SourceSummary,
    StorageUnavailable,
)
from novel_agent.sources.contracts import (
    AuthorizationDeclaration,
    CompleteSourceUpload,
    InitiateSourceUpload,
)

router = APIRouter(prefix="/projects/{project_id}/sources", tags=["sources"])


class SourceApiGateway(Protocol):
    async def initiate(self, **kwargs: object) -> InitiatedUpload: ...
    async def complete(self, **kwargs: object) -> CompletedSource: ...
    async def list_sources(self, **kwargs: object) -> SourcePage: ...
    async def get_source(self, **kwargs: object) -> SourceSummary: ...
    async def start_ingestion(self, **kwargs: object) -> str: ...


class InitiatedUploadResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    source_id: uuid.UUID
    upload_id: uuid.UUID
    upload_url: str
    required_headers: dict[str, str]
    expires_at: datetime


class CompletedSourceResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    source_id: uuid.UUID
    filename: str
    media_type: str
    byte_size: int
    content_sha256: str
    status: str
    accepted_at: datetime


class SourceResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    source_id: uuid.UUID
    filename: str
    media_type: str
    byte_size: int
    content_sha256: str
    status: str
    failure_code: str | None
    created_at: datetime
    accepted_at: datetime | None


class SourcePageResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    items: list[SourceResponse]
    next_cursor: str | None


class AuthorizationDeclarationBody(AuthorizationDeclaration):
    model_config = ConfigDict(strict=False, frozen=True, extra="forbid")


class InitiateUploadBody(InitiateSourceUpload):
    # JSON has no UUID/frozenset native types; domain validation still runs after coercion.
    model_config = ConfigDict(strict=False, frozen=True, extra="forbid")
    authorization: AuthorizationDeclarationBody


class CompleteUploadBody(CompleteSourceUpload):
    model_config = ConfigDict(strict=False, frozen=True, extra="forbid")


class WorkflowResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    workflow_id: str


async def get_source_api_service(request: Request) -> SourceApiService:
    try:
        store = request.app.state.object_store
        workflow_gateway = request.app.state.source_ingestion_workflow_gateway
        factory = request.app.state.session_factory
        settings = request.app.state.settings
    except AttributeError:
        raise StorageUnavailable from None
    return SourceApiService(
        session_factory=factory,
        object_store=store,
        workflow_gateway=workflow_gateway,
        settings=settings,
    )


PrincipalDependency = Annotated[Principal, Depends(get_principal)]
ServiceDependency = Annotated[SourceApiGateway, Depends(get_source_api_service)]


@router.post("/uploads", response_model=InitiatedUploadResponse, status_code=201)
async def initiate_upload(
    project_id: uuid.UUID,
    command: InitiateUploadBody,
    principal: PrincipalDependency,
    service: ServiceDependency,
) -> InitiatedUpload:
    return await service.initiate(
        tenant_id=principal.tenant_id,
        project_id=project_id,
        actor_id=principal.subject,
        command=command,
    )


@router.post("/uploads/{upload_id}/complete", response_model=CompletedSourceResponse)
async def complete_upload(
    project_id: uuid.UUID,
    upload_id: uuid.UUID,
    command: CompleteUploadBody,
    principal: PrincipalDependency,
    service: ServiceDependency,
) -> CompletedSource:
    if command.upload_id != upload_id:
        from novel_agent.sources.errors import SourceConflictError

        raise SourceConflictError("path upload ID differs from body")
    return await service.complete(
        tenant_id=principal.tenant_id,
        project_id=project_id,
        actor_id=principal.subject,
        command=command,
    )


@router.get("", response_model=SourcePageResponse)
async def list_sources(
    project_id: uuid.UUID,
    principal: PrincipalDependency,
    service: ServiceDependency,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query(min_length=1, max_length=512)] = None,
) -> SourcePage:
    return await service.list_sources(
        tenant_id=principal.tenant_id,
        project_id=project_id,
        limit=limit,
        cursor=cursor,
    )


@router.get("/{source_id}", response_model=SourceResponse)
async def get_source(
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    principal: PrincipalDependency,
    service: ServiceDependency,
) -> SourceSummary:
    return await service.get_source(
        tenant_id=principal.tenant_id, project_id=project_id, source_id=source_id
    )


@router.post(
    "/{source_id}/ingestions", response_model=WorkflowResponse, status_code=status.HTTP_202_ACCEPTED
)
async def start_ingestion(
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    principal: PrincipalDependency,
    service: ServiceDependency,
) -> WorkflowResponse:
    workflow_id = await service.start_ingestion(
        tenant_id=principal.tenant_id, project_id=project_id, source_id=source_id
    )
    return WorkflowResponse(workflow_id=workflow_id)


__all__ = ["get_source_api_service", "router"]
