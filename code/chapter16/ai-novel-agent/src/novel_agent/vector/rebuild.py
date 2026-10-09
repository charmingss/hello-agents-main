from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol, cast

from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novel_agent.db.models.source import SourceChunk, SourceDocument, SourceParseRun, SourceSection
from novel_agent.embeddings.ports import EmbeddingIdentity
from novel_agent.outbox.projector import (
    AuthoritativeChunk,
    AuthoritativeProduct,
    ProjectionWorkerError,
)
from novel_agent.vector.rebuild_checkpoints import RebuildAttempt, RebuildCheckpoints
from novel_agent.vector.schema import TenantProjectScope

_FULL_REBUILD_CAPABILITY = object()


@dataclass(frozen=True, init=False)
class FullRebuildAuthorization:
    """An explicit composition-root capability; never construct from an API body."""

    issued_by: str

    def __init__(self, token: object, issued_by: str) -> None:
        if token is not _FULL_REBUILD_CAPABILITY:
            raise PermissionError("full rebuild capability cannot be constructed by callers")
        if not issued_by.strip():
            raise ValueError("full rebuild authorization issuer is required")
        object.__setattr__(self, "issued_by", issued_by)


def issue_full_rebuild_authorization(*, issued_by: str) -> FullRebuildAuthorization:
    """Composition-root-only factory; never expose this function through an HTTP dependency."""
    return FullRebuildAuthorization(_FULL_REBUILD_CAPABILITY, issued_by)


@dataclass(frozen=True)
class ActiveProductHeader:
    scope: TenantProjectScope
    source_document_id: uuid.UUID
    parse_run_id: uuid.UUID
    product_version: int
    product_sha256: str

    @classmethod
    def from_product(cls, product: AuthoritativeProduct) -> ActiveProductHeader:
        return cls(
            product.scope,
            product.source_document_id,
            product.parse_run_id,
            product.product_version,
            product.product_sha256,
        )


class RebuildAuthority(Protocol):
    async def page_active_products(
        self,
        scope: TenantProjectScope | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[tuple[ActiveProductHeader, ...], str | None]: ...

    async def load_product(self, header: ActiveProductHeader) -> AuthoritativeProduct | None: ...


class ProductProjector(Protocol):
    @property
    def embedding(self) -> EmbeddingIdentity: ...

    async def project_product(self, product: AuthoritativeProduct) -> None: ...


class ProjectionRebuilder:
    """Keyset-paged rebuild. Scope is retained on every authority and vector call."""

    def __init__(
        self,
        authority: RebuildAuthority,
        projector: ProductProjector,
        checkpoints: RebuildCheckpoints,
        *,
        page_size: int = 50,
    ) -> None:
        if not 1 <= page_size <= 500:
            raise ValueError("rebuild page size must be between 1 and 500")
        if projector.embedding != checkpoints.embedding:
            raise ValueError("rebuild projection and checkpoint embedding identities differ")
        self._authority = authority
        self._projector = projector
        self._checkpoints = checkpoints
        self._page_size = page_size

    async def rebuild_project(
        self,
        scope: TenantProjectScope,
        *,
        canceled: Callable[[], bool] = lambda: False,
        heartbeat: Callable[[int], None] = lambda count: None,
    ) -> int:
        if not isinstance(scope, TenantProjectScope):
            raise ValueError("valid tenant project scope is required")
        return await self._run(scope, canceled=canceled, heartbeat=heartbeat)

    async def rebuild_all(
        self,
        authorization: FullRebuildAuthorization,
        *,
        canceled: Callable[[], bool] = lambda: False,
        heartbeat: Callable[[int], None] = lambda count: None,
    ) -> int:
        if (
            not isinstance(authorization, FullRebuildAuthorization)
            or not authorization.issued_by.strip()
        ):
            raise ValueError("full rebuild authorization is invalid")
        return await self._run(None, canceled=canceled, heartbeat=heartbeat)

    async def _run(
        self,
        scope: TenantProjectScope | None,
        *,
        canceled: Callable[[], bool],
        heartbeat: Callable[[int], None],
    ) -> int:
        cursor: str | None = None
        projected = 0
        while True:
            if canceled():
                raise asyncio.CancelledError
            products, next_cursor = await self._authority.page_active_products(
                scope, cursor, self._page_size
            )
            if scope is not None and any(header.scope != scope for header in products):
                raise RuntimeError("rebuild_authority_scope_mismatch")
            for header in products:
                if canceled():
                    raise asyncio.CancelledError
                product = await self._authority.load_product(header)
                if product is None:
                    continue
                if ActiveProductHeader.from_product(product) != header:
                    raise RuntimeError("rebuild_authority_product_mismatch")
                attempt = await self._checkpoints.begin(product)
                try:
                    if canceled():
                        raise asyncio.CancelledError
                    await self._projector.project_product(product)
                    if canceled():
                        raise asyncio.CancelledError
                    await self._checkpoints.succeed(product, attempt)
                except asyncio.CancelledError:
                    await self._record_failure(product, attempt, "projection_unavailable")
                    raise
                except Exception as error:
                    code = (
                        error.code
                        if isinstance(error, ProjectionWorkerError)
                        else "projection_unavailable"
                    )
                    await self._record_failure(product, attempt, code)
                    raise ProjectionWorkerError(code) from None
                finally:
                    del product
                projected += 1
                heartbeat(projected)
            if next_cursor is None:
                return projected
            if next_cursor == cursor:
                raise RuntimeError("rebuild_cursor_did_not_advance")
            cursor = next_cursor

    async def _record_failure(
        self, product: AuthoritativeProduct, attempt: RebuildAttempt, code: str
    ) -> None:
        try:
            await self._checkpoints.fail(product, attempt, code)
        except Exception:
            # A superseding attempt must not mask the original projection failure/cancellation.
            pass


class SqlAlchemyRebuildAuthority:
    """Pages active product headers; chunks are materialized one source at a time."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def page_active_products(
        self,
        scope: TenantProjectScope | None,
        cursor: str | None,
        limit: int,
    ) -> tuple[tuple[ActiveProductHeader, ...], str | None]:
        if not 1 <= limit <= 500:
            raise ValueError("rebuild page limit must be between 1 and 500")
        after = None
        if cursor is not None:
            try:
                after = uuid.UUID(cursor)
            except ValueError:
                raise ValueError("invalid rebuild cursor") from None
        statement = (
            select(
                SourceDocument.tenant_id,
                SourceDocument.project_id,
                SourceDocument.id,
                SourceParseRun.id,
                SourceParseRun.product_version,
                SourceParseRun.product_sha256,
            )
            .join(
                SourceParseRun,
                and_(
                    SourceParseRun.tenant_id == SourceDocument.tenant_id,
                    SourceParseRun.project_id == SourceDocument.project_id,
                    SourceParseRun.id == SourceDocument.active_parse_run_id,
                    SourceParseRun.source_document_id == SourceDocument.id,
                ),
            )
            .where(
                SourceDocument.status == "chunks_ready",
                SourceParseRun.status == "succeeded",
                SourceParseRun.product_version == SourceDocument.aggregate_version,
            )
            .order_by(SourceDocument.id)
            .limit(limit + 1)
        )
        if scope is not None:
            statement = statement.where(
                SourceDocument.tenant_id == scope.tenant_id,
                SourceDocument.project_id == scope.project_id,
            )
        if after is not None:
            statement = statement.where(SourceDocument.id > after)
        async with self._session_factory() as session:
            headers = (await session.execute(statement)).all()
            selected = headers[:limit]
            products = tuple(
                ActiveProductHeader(
                    TenantProjectScope(tenant_id=tenant, project_id=project),
                    source,
                    run,
                    version,
                    digest,
                )
                for tenant, project, source, run, version, digest in selected
            )
        if scope is not None and any(header.scope != scope for header in products):
            raise RuntimeError("rebuild_authority_scope_mismatch")
        next_cursor = (
            str(products[-1].source_document_id) if len(headers) > limit and products else None
        )
        return products, next_cursor

    async def load_product(self, header: ActiveProductHeader) -> AuthoritativeProduct | None:
        # Hold only database locks while materializing this exact source; release before embedding.
        async with self._session_factory() as session, session.begin():
            source = await session.scalar(
                select(SourceDocument)
                .where(
                    SourceDocument.tenant_id == header.scope.tenant_id,
                    SourceDocument.project_id == header.scope.project_id,
                    SourceDocument.id == header.source_document_id,
                )
                .with_for_update()
            )
            if (
                source is None
                or source.status != "chunks_ready"
                or source.active_parse_run_id != header.parse_run_id
                or source.aggregate_version != header.product_version
            ):
                return None
            run = await session.scalar(
                select(SourceParseRun)
                .where(
                    SourceParseRun.tenant_id == header.scope.tenant_id,
                    SourceParseRun.project_id == header.scope.project_id,
                    SourceParseRun.source_document_id == header.source_document_id,
                    SourceParseRun.id == header.parse_run_id,
                )
                .with_for_update()
            )
            if (
                run is None
                or run.status != "succeeded"
                or run.product_version != header.product_version
                or run.product_sha256 != header.product_sha256
            ):
                return None
            return await self._load_product(session, source, run)

    @staticmethod
    async def _load_product(
        session: AsyncSession, source: SourceDocument, run: SourceParseRun
    ) -> AuthoritativeProduct:
        rows = (
            await session.execute(
                select(SourceChunk, SourceSection.heading)
                .join(
                    SourceSection,
                    and_(
                        SourceSection.tenant_id == SourceChunk.tenant_id,
                        SourceSection.project_id == SourceChunk.project_id,
                        SourceSection.parse_run_id == SourceChunk.parse_run_id,
                        SourceSection.id == SourceChunk.section_id,
                    ),
                )
                .where(
                    SourceChunk.tenant_id == source.tenant_id,
                    SourceChunk.project_id == source.project_id,
                    SourceChunk.parse_run_id == run.id,
                )
                .order_by(SourceChunk.chunk_index)
            )
        ).all()
        chunks = tuple(
            AuthoritativeChunk(
                id=chunk.id,
                section_id=chunk.section_id,
                text=chunk.text,
                heading=heading,
                source_ref_kind=cast(
                    Literal["paragraph", "table_row", "page"], chunk.source_ref_kind
                ),
                source_ref_index=cast(int, chunk.source_ref_index),
                source_ref_subindex=chunk.source_ref_subindex,
                char_start=chunk.char_start,
                char_end=chunk.char_end,
            )
            for chunk, heading in rows
        )
        if not chunks:
            raise RuntimeError("active_product_has_no_chunks")
        return AuthoritativeProduct(
            scope=TenantProjectScope(tenant_id=source.tenant_id, project_id=source.project_id),
            source_document_id=source.id,
            parse_run_id=run.id,
            product_version=cast(int, run.product_version),
            product_sha256=cast(str, run.product_sha256),
            source_label=source.original_display_name,
            parser_name=run.parser_name,
            parser_version=run.parser_version,
            parser_config_sha256=run.parser_config_sha256,
            chunker_name=cast(str, run.chunker_name),
            chunker_version=run.chunker_version,
            chunker_config_sha256=cast(str, run.chunker_config_sha256),
            containment_policy=cast(Literal["single-section-v1"], run.containment_policy),
            chunks=chunks,
        )


__all__ = [
    "FullRebuildAuthorization",
    "ProjectionRebuilder",
    "SqlAlchemyRebuildAuthority",
    "issue_full_rebuild_authorization",
]
