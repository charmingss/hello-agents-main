"""Memory API routes: extract/list/get/patch writing memory (Phase 6)."""

import uuid
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from novel_agent.analysis.contracts import AnalysisError
from novel_agent.analysis.memory_service import MemoryPatch, MemoryService
from novel_agent.api.dependencies import get_principal
from novel_agent.auth.models import Principal
from novel_agent.vector.schema import TenantProjectScope

router = APIRouter(tags=["memory"])


def _scope(principal: Principal, project_id: uuid.UUID) -> TenantProjectScope:
    return TenantProjectScope(tenant_id=principal.tenant_id, project_id=project_id)


def _service(request: Request) -> MemoryService:
    return cast(MemoryService, request.app.state.memory_service)


def _raise_analysis_error(exc: AnalysisError) -> None:
    raise HTTPException(exc.status, detail={"code": exc.code}) from None


class ExtractMemoryBody(BaseModel):
    """Body for extract_memory: which chapter to extract from."""

    chapter_index: int


@router.post("/projects/{project_id}/memories/extract")
async def extract_memory(
    project_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
    body: ExtractMemoryBody,
) -> dict[str, Any]:
    try:
        return await _service(request).extract_memory(
            _scope(principal, project_id),
            principal.subject,
            body.chapter_index,
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.post("/projects/{project_id}/sources/{source_id}/memories/extract")
async def extract_source_memory(
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).extract_from_source(
            _scope(principal, project_id),
            principal.subject,
            source_id,
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.get("/projects/{project_id}/memories")
async def list_memories(
    project_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
    status: str | None = None,
) -> dict[str, Any]:
    try:
        return await _service(request).list_memories(
            _scope(principal, project_id),
            status=status,
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.get("/projects/{project_id}/memories/{memory_id}")
async def get_memory(
    project_id: uuid.UUID,
    memory_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        result = await _service(request).get_memory(
            _scope(principal, project_id),
            memory_id,
        )
        if result is None:
            raise HTTPException(404, detail={"code": "memory_not_found"})
        return result
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.patch("/projects/{project_id}/memories/{memory_id}")
async def patch_memory(
    project_id: uuid.UUID,
    memory_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
    patch: MemoryPatch,
) -> dict[str, Any]:
    try:
        return await _service(request).patch_memory(
            _scope(principal, project_id),
            memory_id,
            patch,
            principal.subject,
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable
