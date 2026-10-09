"""Story Graph API routes: relations, events, and graph projection sync."""

import uuid
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request

from novel_agent.analysis.contracts import AnalysisError
from novel_agent.analysis.story_graph import (
    EventCreate,
    EventPatch,
    RelationCreate,
    RelationPatch,
    StoryGraphService,
)
from novel_agent.api.dependencies import get_principal
from novel_agent.auth.models import Principal
from novel_agent.vector.schema import TenantProjectScope

router = APIRouter(tags=["story-graph"])


def _scope(principal: Principal, project_id: uuid.UUID) -> TenantProjectScope:
    return TenantProjectScope(tenant_id=principal.tenant_id, project_id=project_id)


def _service(request: Request) -> StoryGraphService:
    return cast(StoryGraphService, request.app.state.story_graph_service)


def _raise_analysis_error(exc: AnalysisError) -> None:
    raise HTTPException(exc.status, detail={"code": exc.code}) from None


# -- Graph --


@router.get("/projects/{project_id}/graph")
async def get_graph(
    project_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).get_graph(_scope(principal, project_id))
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.post(
    "/projects/{project_id}/story-bible/graph/sync",
    status_code=202,
)
async def sync_graph(
    project_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).sync_scope(_scope(principal, project_id))
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


# -- Relations --


@router.get("/projects/{project_id}/story-bible/relations")
async def list_relations(
    project_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).list_relations(_scope(principal, project_id))
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.post("/projects/{project_id}/story-bible/relations", status_code=201)
async def create_relation(
    project_id: uuid.UUID,
    data: RelationCreate,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).create_relation(
            _scope(principal, project_id), data, principal.subject
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.get("/projects/{project_id}/story-bible/relations/{relation_id}")
async def get_relation(
    project_id: uuid.UUID,
    relation_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).get_relation(
            _scope(principal, project_id), relation_id
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.patch("/projects/{project_id}/story-bible/relations/{relation_id}")
async def patch_relation(
    project_id: uuid.UUID,
    relation_id: uuid.UUID,
    patch: RelationPatch,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).patch_relation(
            _scope(principal, project_id), relation_id, patch, principal.subject
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


# -- Events --


@router.get("/projects/{project_id}/story-bible/events")
async def list_events(
    project_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).list_events(_scope(principal, project_id))
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.post("/projects/{project_id}/story-bible/events", status_code=201)
async def create_event(
    project_id: uuid.UUID,
    data: EventCreate,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).create_event(
            _scope(principal, project_id), data, principal.subject
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.get("/projects/{project_id}/story-bible/events/{event_id}")
async def get_event(
    project_id: uuid.UUID,
    event_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).get_event(
            _scope(principal, project_id), event_id
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.patch("/projects/{project_id}/story-bible/events/{event_id}")
async def patch_event(
    project_id: uuid.UUID,
    event_id: uuid.UUID,
    patch: EventPatch,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).patch_event(
            _scope(principal, project_id), event_id, patch, principal.subject
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable