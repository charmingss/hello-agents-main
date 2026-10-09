"""Chapter API routes: generate/read/patch chapter content (Phase 5).

路径前缀 `/chapters`（整数 order_index），避免与 outline router 的
`/outline/chapters/{chapter_id}`（UUID）在 Starlette 中冲突。
"""

import uuid
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request

from novel_agent.analysis.chapter_service import ChapterPatch, ChapterService
from novel_agent.analysis.contracts import AnalysisError
from novel_agent.api.dependencies import get_principal
from novel_agent.auth.models import Principal
from novel_agent.vector.schema import TenantProjectScope

router = APIRouter(tags=["chapters"])


def _scope(principal: Principal, project_id: uuid.UUID) -> TenantProjectScope:
    return TenantProjectScope(tenant_id=principal.tenant_id, project_id=project_id)


def _service(request: Request) -> ChapterService:
    return cast(ChapterService, request.app.state.chapter_service)


def _raise_analysis_error(exc: AnalysisError) -> None:
    raise HTTPException(exc.status, detail={"code": exc.code}) from None


@router.post("/projects/{project_id}/chapters/{order_index}/generate")
async def generate_chapter(
    project_id: uuid.UUID,
    order_index: int,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).generate_chapter(
            _scope(principal, project_id),
            principal.subject,
            order_index,
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.get("/projects/{project_id}/chapters")
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


@router.get("/projects/{project_id}/chapters/{order_index}")
async def get_chapter(
    project_id: uuid.UUID,
    order_index: int,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        result = await _service(request).get_chapter(
            _scope(principal, project_id),
            order_index,
        )
        if result is None:
            raise HTTPException(404, detail={"code": "chapter_not_found"})
        return result
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.patch("/projects/{project_id}/chapters/{order_index}")
async def patch_chapter(
    project_id: uuid.UUID,
    order_index: int,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
    patch: ChapterPatch,
) -> dict[str, Any]:
    try:
        return await _service(request).patch_chapter(
            _scope(principal, project_id),
            order_index,
            patch,
            principal.subject,
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable
