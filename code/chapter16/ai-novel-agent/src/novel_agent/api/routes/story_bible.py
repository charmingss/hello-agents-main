"""Story Bible API routes: characters, world entries, and foreshadowing."""

import uuid
from typing import Annotated, Any, cast

from fastapi import APIRouter, Depends, HTTPException, Request

from novel_agent.analysis.contracts import AnalysisError
from novel_agent.analysis.story_bible import (
    CharacterCreate,
    CharacterPatch,
    ForeshadowingCreate,
    ForeshadowingPatch,
    ForeshadowingResolve,
    StoryBibleService,
    StyleProfilePatch,
    WorldEntryCreate,
    WorldEntryPatch,
)
from novel_agent.api.dependencies import get_principal
from novel_agent.auth.models import Principal
from novel_agent.vector.schema import TenantProjectScope

router = APIRouter(tags=["story-bible"])


def _scope(principal: Principal, project_id: uuid.UUID) -> TenantProjectScope:
    return TenantProjectScope(tenant_id=principal.tenant_id, project_id=project_id)


def _service(request: Request) -> StoryBibleService:
    return cast(StoryBibleService, request.app.state.story_bible_service)


def _raise_analysis_error(exc: AnalysisError) -> None:
    raise HTTPException(exc.status, detail={"code": exc.code}) from None


# -- Story Bible --


@router.get("/projects/{project_id}/story-bible")
async def get_story_bible(
    project_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).get_bible(_scope(principal, project_id))
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


# -- Characters --


@router.get("/projects/{project_id}/story-bible/characters")
async def list_characters(
    project_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).list_characters(_scope(principal, project_id))
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.post("/projects/{project_id}/story-bible/characters", status_code=201)
async def create_character(
    project_id: uuid.UUID,
    data: CharacterCreate,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).create_character(
            _scope(principal, project_id), data, principal.subject
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.get("/projects/{project_id}/story-bible/characters/{character_id}")
async def get_character(
    project_id: uuid.UUID,
    character_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).get_character(
            _scope(principal, project_id), character_id
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.patch("/projects/{project_id}/story-bible/characters/{character_id}")
async def patch_character(
    project_id: uuid.UUID,
    character_id: uuid.UUID,
    patch: CharacterPatch,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).patch_character(
            _scope(principal, project_id), character_id, patch, principal.subject
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.post(
    "/projects/{project_id}/sources/{source_id}/analyses/{analysis_id}"
    "/characters/{character_id}/promote",
    status_code=201,
)
async def promote_character(
    project_id: uuid.UUID,
    source_id: uuid.UUID,
    analysis_id: uuid.UUID,
    character_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).promote_character(
            _scope(principal, project_id),
            source_id,
            analysis_id,
            character_id,
            principal.subject,
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


# -- World Entries --


@router.get("/projects/{project_id}/story-bible/world-entries")
async def list_world_entries(
    project_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).list_world_entries(_scope(principal, project_id))
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.post("/projects/{project_id}/story-bible/world-entries", status_code=201)
async def create_world_entry(
    project_id: uuid.UUID,
    data: WorldEntryCreate,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).create_world_entry(
            _scope(principal, project_id), data, principal.subject
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.get("/projects/{project_id}/story-bible/world-entries/{entry_id}")
async def get_world_entry(
    project_id: uuid.UUID,
    entry_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).get_world_entry(
            _scope(principal, project_id), entry_id
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.patch("/projects/{project_id}/story-bible/world-entries/{entry_id}")
async def patch_world_entry(
    project_id: uuid.UUID,
    entry_id: uuid.UUID,
    patch: WorldEntryPatch,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).patch_world_entry(
            _scope(principal, project_id), entry_id, patch, principal.subject
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


# -- Foreshadowing --


@router.get("/projects/{project_id}/story-bible/foreshadowing")
async def list_foreshadowing(
    project_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).list_foreshadowing(_scope(principal, project_id))
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.post("/projects/{project_id}/story-bible/foreshadowing", status_code=201)
async def create_foreshadowing(
    project_id: uuid.UUID,
    data: ForeshadowingCreate,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).create_foreshadowing(
            _scope(principal, project_id), data, principal.subject
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.get(
    "/projects/{project_id}/story-bible/foreshadowing/{foreshadowing_id}"
)
async def get_foreshadowing(
    project_id: uuid.UUID,
    foreshadowing_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).get_foreshadowing(
            _scope(principal, project_id), foreshadowing_id
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.patch(
    "/projects/{project_id}/story-bible/foreshadowing/{foreshadowing_id}"
)
async def patch_foreshadowing(
    project_id: uuid.UUID,
    foreshadowing_id: uuid.UUID,
    patch: ForeshadowingPatch,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).patch_foreshadowing(
            _scope(principal, project_id), foreshadowing_id, patch, principal.subject
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.post(
    "/projects/{project_id}/story-bible/foreshadowing/{foreshadowing_id}/resolve"
)
async def resolve_foreshadowing(
    project_id: uuid.UUID,
    foreshadowing_id: uuid.UUID,
    data: ForeshadowingResolve,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).resolve_foreshadowing(
            _scope(principal, project_id), foreshadowing_id, data, principal.subject
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


# -- Style Profile --


@router.get("/projects/{project_id}/story-bible/style")
async def get_style_profile(
    project_id: uuid.UUID,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        result = await _service(request).get_style_profile(_scope(principal, project_id))
        if result is None:
            raise AnalysisError("style_not_found", 404)
        return result
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable


@router.patch("/projects/{project_id}/story-bible/style")
async def patch_style_profile(
    project_id: uuid.UUID,
    patch: StyleProfilePatch,
    request: Request,
    principal: Annotated[Principal, Depends(get_principal)],
) -> dict[str, Any]:
    try:
        return await _service(request).patch_style_profile(
            _scope(principal, project_id), patch, principal.subject
        )
    except AnalysisError as exc:
        _raise_analysis_error(exc)
        raise  # unreachable
