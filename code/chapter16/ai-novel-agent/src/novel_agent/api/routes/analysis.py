import uuid
from dataclasses import asdict
from typing import Annotated, Any, Never, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import JSONResponse

from novel_agent.analysis.contracts import AnalysisError
from novel_agent.analysis.history import CharacterReviewPatch, SourceAnalysisHistoryService
from novel_agent.analysis.jobs import FullAnalysisJobService, FullAnalysisJobStatus
from novel_agent.analysis.service import SourceAnalysisService
from novel_agent.api.dependencies import get_principal
from novel_agent.auth.models import Principal
from novel_agent.vector.schema import TenantProjectScope

router = APIRouter(tags=["analysis"])


def _scope(principal: Principal, project_id: uuid.UUID) -> TenantProjectScope:
    return TenantProjectScope(tenant_id=principal.tenant_id, project_id=project_id)


def _history(request: Request) -> SourceAnalysisHistoryService:
    return cast(SourceAnalysisHistoryService, request.app.state.source_analysis_history_service)


def _raise_analysis_error(exc: AnalysisError) -> Never:
    raise HTTPException(exc.status, detail={"code": exc.code}) from None


async def _require_empty_post(request: Request) -> None:
    if request.query_params or request.headers.get("content-length", "0") != "0":
        raise HTTPException(422, detail={"code": "invalid_request"})
    async for chunk in request.stream():
        if chunk:
            raise HTTPException(422, detail={"code": "invalid_request"})


@router.post("/projects/{project_id}/sources/{source_id}/analysis-preview")
async def source_analysis_preview(
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    # No client-controlled model, tenant, or analysis options, even in an otherwise unused body.
    await _require_empty_post(request)
    service = cast(SourceAnalysisService, request.app.state.source_analysis_service)
    try:
        return await service.preview(
            TenantProjectScope(tenant_id=principal.tenant_id, project_id=project_id), source_id
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)


@router.post("/projects/{project_id}/sources/{source_id}/analyses")
async def create_source_analysis(
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    await _require_empty_post(request)
    try:
        return await _history(request).create(
            _scope(principal, project_id), source_id, principal.subject
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)


@router.get("/projects/{project_id}/sources/{source_id}/analyses")
async def list_source_analyses(
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    cursor: str | None = None,
) -> dict[str, Any]:
    try:
        return await _history(request).list(
            _scope(principal, project_id), source_id, limit=limit, cursor=cursor
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)


@router.get("/projects/{project_id}/sources/{source_id}/analyses/{analysis_id}")
async def get_source_analysis(
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    analysis_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _history(request).get(_scope(principal, project_id), source_id, analysis_id)
    except AnalysisError as exc:
        _raise_analysis_error(exc)


@router.patch(
    "/projects/{project_id}/sources/{source_id}/analyses/{analysis_id}/characters/{character_id}"
)
async def review_source_analysis_character(
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    analysis_id: uuid.UUID,
    character_id: uuid.UUID,
    patch: CharacterReviewPatch,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _history(request).review(
            _scope(principal, project_id), source_id, analysis_id, character_id, patch,
            principal.subject,
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)


def _job_service(request: Request) -> FullAnalysisJobService:
    return cast(FullAnalysisJobService, request.app.state.full_analysis_job_service)


def _job_status_dict(status: FullAnalysisJobStatus | dict[str, Any]) -> dict[str, Any]:
    if isinstance(status, dict):
        return status
    return asdict(status)


@router.post(
    "/projects/{project_id}/sources/{source_id}/analysis-jobs",
    status_code=202,
)
async def create_analysis_job(
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> JSONResponse:
    await _require_empty_post(request)
    try:
        status = await _job_service(request).create(
            _scope(principal, project_id), source_id, principal.subject
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable
    return JSONResponse(status_code=202, content=_job_status_dict(status))


@router.get(
    "/projects/{project_id}/sources/{source_id}/analysis-jobs/{job_id}"
)
async def get_analysis_job(
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    job_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        status = await _job_service(request).status(
            _scope(principal, project_id), source_id, job_id
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable
    return _job_status_dict(status)


@router.post(
    "/projects/{project_id}/sources/{source_id}/analysis-jobs/{job_id}/retry",
    status_code=202,
)
async def retry_analysis_job(
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    job_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> JSONResponse:
    await _require_empty_post(request)
    try:
        status = await _job_service(request).retry(
            _scope(principal, project_id), source_id, job_id
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable
    return JSONResponse(status_code=202, content=_job_status_dict(status))
