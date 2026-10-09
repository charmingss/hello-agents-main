"""Outline API routes: generate/patch outline and chapters (Phase 4)."""

import uuid
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from novel_agent.analysis.contracts import AnalysisError
from novel_agent.analysis.outline_service import (
    ChapterPatch,
    OutlinePatch,
    OutlineService,
)
from novel_agent.api.dependencies import get_principal
from novel_agent.auth.models import Principal
from novel_agent.vector.schema import TenantProjectScope

router = APIRouter(tags=["outline"])


def _scope(principal: Principal, project_id: uuid.UUID) -> TenantProjectScope:
    return TenantProjectScope(tenant_id=principal.tenant_id, project_id=project_id)


def _service(request: Request) -> OutlineService:
    return cast(OutlineService, request.app.state.outline_service)


def _raise_analysis_error(exc: AnalysisError) -> None:
    raise HTTPException(exc.status, detail={"code": exc.code}) from None


class _GenerateBody(BaseModel):
    """Optional body for generate_outline."""

    target_chapters: Annotated[int, Field(ge=1, le=500)] = 10
    user_prompt: Annotated[str, Field(max_length=4000)] | None = None


@router.post("/projects/{project_id}/outline/generate")
async def generate_outline(
    project_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
    body: _GenerateBody | None = None,
) -> dict[str, Any]:
    try:
        target = body.target_chapters if body is not None else 10
        prompt = body.user_prompt if body is not None else None
        return await _service(request).generate_outline(
            _scope(principal, project_id),
            principal.subject,
            target_chapters=target,
            user_prompt=prompt,
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.get("/projects/{project_id}/outline")
async def get_outline(
    project_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        result = await _service(request).get_outline(_scope(principal, project_id))
        if result is None:
            raise HTTPException(404, detail={"code": "outline_not_found"})
        return result
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.patch("/projects/{project_id}/outline")
async def patch_outline(
    project_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
    patch: OutlinePatch,
) -> dict[str, Any]:
    try:
        return await _service(request).patch_outline(
            _scope(principal, project_id),
            patch,
            principal.subject,
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.get("/projects/{project_id}/outline/chapters")
async def list_chapters(
    project_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).list_chapters(_scope(principal, project_id))
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.patch("/projects/{project_id}/outline/chapters/{chapter_id}")
async def patch_chapter(
    project_id: uuid.UUID,
    chapter_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
    patch: ChapterPatch,
) -> dict[str, Any]:
    try:
        return await _service(request).patch_chapter(
            _scope(principal, project_id),
            chapter_id,
            patch,
            principal.subject,
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable
