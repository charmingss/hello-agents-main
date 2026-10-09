from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal, Protocol, cast

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novel_agent.api.dependencies import get_principal
from novel_agent.api.errors import ProjectNotFoundError
from novel_agent.auth.models import Principal
from novel_agent.db.models.project import Project
from novel_agent.db.models.source import (
    ProjectionCheckpoint,
    SourceChunk,
    SourceDocument,
    SourceParseRun,
    SourceSection,
)
from novel_agent.embeddings.ports import EmbeddingIdentity, EmbeddingProvider, embed_bounded
from novel_agent.outbox.projector import embedding_identity_sha256
from novel_agent.vector.schema import (
    VECTOR_SCHEMA_VERSION,
    ProjectionFilter,
    SourceCitation,
    TenantProjectScope,
    VectorSearchHit,
)

router = APIRouter(prefix="/projects/{project_id}/search", tags=["search"])

SearchReason = Literal[
    "search_not_configured",
    "embedding_unavailable",
    "projection_unavailable",
    "projection_lagging",
]


class SearchBody(BaseModel):
    model_config = ConfigDict(strict=False, frozen=True, extra="forbid")
    query: Annotated[str, StringConstraints(min_length=1, max_length=4000)]
    limit: Annotated[int, Field(ge=1, le=100)] = 20
    filter: ProjectionFilter | None = None

    @field_validator("query")
    @classmethod
    def query_must_not_be_blank(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("query must not be blank")
        return value


class SearchResponse(BaseModel):
    model_config = ConfigDict(frozen=True)
    hits: list[VectorSearchHit]
    degraded: bool
    reason: SearchReason | None
    staleness: datetime | None


class SourceSearchGateway(Protocol):
    async def search(
        self,
        *,
        scope: TenantProjectScope,
        query: str,
        limit: int,
        where: ProjectionFilter | None,
    ) -> SearchResponse: ...


class SearchAuthority(Protocol):
    async def require_project(self, scope: TenantProjectScope) -> None: ...

    async def filter_current_hits(
        self, scope: TenantProjectScope, hits: tuple[VectorSearchHit, ...]
    ) -> tuple[VectorSearchHit, ...]: ...

    async def projection_status(
        self, scope: TenantProjectScope, identity: EmbeddingIdentity
    ) -> tuple[bool, datetime | None]: ...


class SearchProjection(Protocol):
    embedding: EmbeddingIdentity

    async def search(
        self,
        scope: TenantProjectScope,
        vector: tuple[float, ...],
        *,
        limit: int,
        where: ProjectionFilter | None = None,
    ) -> tuple[VectorSearchHit, ...]: ...


class SourceSearchService:
    def __init__(
        self,
        authority: SearchAuthority,
        embedding: EmbeddingProvider,
        projection: SearchProjection,
    ) -> None:
        if projection.embedding != embedding.identity:
            raise ValueError("search embedding identity differs from projection")
        self._authority = authority
        self._embedding = embedding
        self._projection = projection

    async def search(
        self,
        *,
        scope: TenantProjectScope,
        query: str,
        limit: int,
        where: ProjectionFilter | None,
    ) -> SearchResponse:
        await self._authority.require_project(scope)
        current, staleness = await self._authority.projection_status(
            scope, self._embedding.identity
        )
        if not current:
            return SearchResponse(
                hits=[], degraded=True, reason="projection_lagging", staleness=staleness
            )
        try:
            vector = (await embed_bounded(self._embedding, (query,)))[0]
        except Exception:
            return SearchResponse(
                hits=[], degraded=True, reason="embedding_unavailable", staleness=staleness
            )
        try:
            candidates = await self._projection.search(scope, vector, limit=limit, where=where)
        except Exception:
            return SearchResponse(
                hits=[], degraded=True, reason="projection_unavailable", staleness=staleness
            )
        hits = await self._authority.filter_current_hits(scope, candidates)
        current, staleness = await self._authority.projection_status(
            scope, self._embedding.identity
        )
        degraded = not current or len(hits) != len(candidates)
        return SearchResponse(
            hits=list(hits) if current else [],
            degraded=degraded,
            reason="projection_lagging" if degraded else None,
            staleness=staleness,
        )


class SqlAlchemySearchAuthority:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def require_project(self, scope: TenantProjectScope) -> None:
        async with self._session_factory() as session:
            found = await session.scalar(
                select(Project.id).where(
                    Project.tenant_id == scope.tenant_id,
                    Project.id == scope.project_id,
                    Project.status == "active",
                )
            )
        if found is None:
            raise ProjectNotFoundError

    async def filter_current_hits(
        self, scope: TenantProjectScope, hits: tuple[VectorSearchHit, ...]
    ) -> tuple[VectorSearchHit, ...]:
        if not hits:
            return ()
        chunk_ids = tuple(hit.citation.chunk_id for hit in hits)
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(
                        SourceChunk,
                        SourceParseRun,
                        SourceDocument,
                        SourceSection.heading,
                    )
                    .select_from(SourceChunk)
                    .join(
                        SourceParseRun,
                        and_(
                            SourceParseRun.tenant_id == SourceChunk.tenant_id,
                            SourceParseRun.project_id == SourceChunk.project_id,
                            SourceParseRun.id == SourceChunk.parse_run_id,
                        ),
                    )
                    .join(
                        SourceSection,
                        and_(
                            SourceSection.tenant_id == SourceChunk.tenant_id,
                            SourceSection.project_id == SourceChunk.project_id,
                            SourceSection.parse_run_id == SourceChunk.parse_run_id,
                            SourceSection.id == SourceChunk.section_id,
                        ),
                    )
                    .join(
                        SourceDocument,
                        and_(
                            SourceDocument.tenant_id == SourceParseRun.tenant_id,
                            SourceDocument.project_id == SourceParseRun.project_id,
                            SourceDocument.id == SourceParseRun.source_document_id,
                            SourceDocument.active_parse_run_id == SourceParseRun.id,
                        ),
                    )
                    .where(
                        SourceChunk.tenant_id == scope.tenant_id,
                        SourceChunk.project_id == scope.project_id,
                        SourceChunk.id.in_(chunk_ids),
                        SourceDocument.status == "chunks_ready",
                        SourceParseRun.status == "succeeded",
                    )
                )
            ).all()
        authoritative = {
            (
                chunk.id,
                source.id,
                chunk.section_id,
                run.id,
                run.product_version,
                run.product_sha256,
            ): (
                chunk.text,
                SourceCitation(
                    source_document_id=source.id,
                    parse_run_id=run.id,
                    product_version=cast(int, run.product_version),
                    product_sha256=cast(str, run.product_sha256),
                    section_id=chunk.section_id,
                    chunk_id=chunk.id,
                    citation_label=source.original_display_name,
                    citation_heading=heading,
                    source_ref_kind=cast(
                        Literal["paragraph", "table_row", "page"], chunk.source_ref_kind
                    ),
                    source_ref_index=cast(int, chunk.source_ref_index),
                    source_ref_subindex=chunk.source_ref_subindex,
                    char_start=chunk.char_start,
                    char_end=chunk.char_end,
                    parser_name=run.parser_name,
                    parser_version=run.parser_version,
                    parser_config_sha256=run.parser_config_sha256,
                    chunker_name=run.chunker_name,
                    chunker_version=run.chunker_version,
                    chunker_config_sha256=run.chunker_config_sha256,
                    containment_policy=cast(Literal["single-section-v1"], run.containment_policy),
                ),
            )
            for chunk, run, source, heading in rows
        }
        return tuple(
            VectorSearchHit(
                point_id=hit.point_id,
                score=hit.score,
                text=authoritative[key][0],
                citation=authoritative[key][1],
            )
            for hit in hits
            if (
                key := (
                    hit.citation.chunk_id,
                    hit.citation.source_document_id,
                    hit.citation.section_id,
                    hit.citation.parse_run_id,
                    hit.citation.product_version,
                    hit.citation.product_sha256,
                )
            )
            in authoritative
        )

    async def projection_status(
        self, scope: TenantProjectScope, identity: EmbeddingIdentity
    ) -> tuple[bool, datetime | None]:
        identity_hash = embedding_identity_sha256(identity)
        async with self._session_factory() as session:
            rows = (
                await session.execute(
                    select(SourceDocument, SourceParseRun, ProjectionCheckpoint)
                    .select_from(SourceDocument)
                    .outerjoin(
                        SourceParseRun,
                        and_(
                            SourceParseRun.tenant_id == SourceDocument.tenant_id,
                            SourceParseRun.project_id == SourceDocument.project_id,
                            SourceParseRun.source_document_id == SourceDocument.id,
                            SourceParseRun.id == SourceDocument.active_parse_run_id,
                        ),
                    )
                    .outerjoin(
                        ProjectionCheckpoint,
                        and_(
                            ProjectionCheckpoint.tenant_id == SourceDocument.tenant_id,
                            ProjectionCheckpoint.project_id == SourceDocument.project_id,
                            SourceDocument.id == ProjectionCheckpoint.source_document_id,
                            ProjectionCheckpoint.projection_name == "qdrant_source_chunks",
                            ProjectionCheckpoint.embedding_identity_sha256 == identity_hash,
                        ),
                    )
                    .where(
                        SourceDocument.tenant_id == scope.tenant_id,
                        SourceDocument.project_id == scope.project_id,
                        SourceDocument.status == "chunks_ready",
                    )
                )
            ).all()
        current = True
        timestamps = []
        for source, run, checkpoint in rows:
            matches = (
                run is not None
                and run.status == "succeeded"
                and run.product_version is not None
                and run.product_sha256 is not None
                and run.product_version == source.aggregate_version
                and checkpoint is not None
                and checkpoint.status == "current"
                and checkpoint.source_document_id == source.id
                and checkpoint.parse_run_id == run.id
                and checkpoint.product_sha256 == run.product_sha256
                and checkpoint.projection_version == source.aggregate_version
                and checkpoint.embedding_identity_sha256 == identity_hash
                and checkpoint.vector_schema_version == VECTOR_SCHEMA_VERSION
            )
            current = current and matches
            if checkpoint is not None and checkpoint.last_succeeded_at is not None:
                timestamps.append(checkpoint.last_succeeded_at)
        return current, min(timestamps) if timestamps else None


class UnconfiguredSearchService:
    async def search(
        self,
        *,
        scope: TenantProjectScope,
        query: str,
        limit: int,
        where: ProjectionFilter | None,
    ) -> SearchResponse:
        del scope, query, limit, where
        return SearchResponse(
            hits=[], degraded=True, reason="search_not_configured", staleness=None
        )


def get_source_search_service(request: Request) -> SourceSearchGateway:
    return cast(SourceSearchGateway, request.app.state.source_search_service)


PrincipalDependency = Annotated[Principal, Depends(get_principal)]
ServiceDependency = Annotated[SourceSearchGateway, Depends(get_source_search_service)]


@router.post("", response_model=SearchResponse)
async def source_search(
    project_id: uuid.UUID,
    body: SearchBody,
    principal: PrincipalDependency,
    service: ServiceDependency,
) -> SearchResponse:
    return await service.search(
        scope=TenantProjectScope(tenant_id=principal.tenant_id, project_id=project_id),
        query=body.query,
        limit=body.limit,
        where=body.filter,
    )


__all__ = [
    "SearchBody",
    "SearchResponse",
    "SourceSearchService",
    "SqlAlchemySearchAuthority",
    "UnconfiguredSearchService",
    "get_source_search_service",
    "router",
]
