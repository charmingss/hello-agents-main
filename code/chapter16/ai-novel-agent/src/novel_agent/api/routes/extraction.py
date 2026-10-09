"""Extraction API routes: extract events/relations/style from analysis results."""

import uuid
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request

from novel_agent.analysis.contracts import AnalysisError
from novel_agent.analysis.extraction_service import ExtractionService
from novel_agent.api.dependencies import get_principal
from novel_agent.auth.models import Principal
from novel_agent.vector.schema import TenantProjectScope

router = APIRouter(tags=["extraction"])


def _scope(principal: Principal, project_id: uuid.UUID) -> TenantProjectScope:
    return TenantProjectScope(tenant_id=principal.tenant_id, project_id=project_id)


def _service(request: Request) -> ExtractionService:
    return cast(ExtractionService, request.app.state.extraction_service)


def _raise_analysis_error(exc: AnalysisError) -> None:
    raise HTTPException(exc.status, detail={"code": exc.code}) from None


@router.post(
    "/projects/{project_id}/sources/{source_id}/analyses/{analysis_id}/extract"
)
async def extract(
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    analysis_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        summary = await _service(request).extract(
            _scope(principal, project_id),
            source_id,
            analysis_id,
            principal.subject,
        )
        return summary.model_dump(mode="json")
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable
