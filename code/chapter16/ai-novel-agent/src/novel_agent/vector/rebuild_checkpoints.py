from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novel_agent.db.models.outbox import OutboxEvent
from novel_agent.db.models.source import ProjectionCheckpoint, SourceDocument, SourceParseRun
from novel_agent.embeddings.ports import EmbeddingIdentity
from novel_agent.outbox.projector import (
    AuthoritativeProduct,
    ProjectionCheckpointCASConflict,
    ProjectionWorkerError,
    SqlAlchemyProjectionCheckpoints,
    embedding_identity_sha256,
)
from novel_agent.vector.schema import VECTOR_SCHEMA_VERSION


@dataclass(frozen=True)
class RebuildAttempt:
    token: uuid.UUID
    event_id: uuid.UUID
    occurred_at: datetime


class RebuildCheckpoints(Protocol):
    @property
    def embedding(self) -> EmbeddingIdentity: ...

    async def begin(self, product: AuthoritativeProduct) -> RebuildAttempt: ...

    async def succeed(self, product: AuthoritativeProduct, attempt: RebuildAttempt) -> None: ...

    async def fail(
        self, product: AuthoritativeProduct, attempt: RebuildAttempt, code: str
    ) -> None: ...


class SqlAlchemyRebuildCheckpoints:
    """Independent rebuild attempts; never acquire or modify delivery leases.

    All transitions lock Source -> Run -> Checkpoint. The source lock also
    serializes checkpoint creation with the outbox worker's existing lock order.
    """

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        identity: EmbeddingIdentity,
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._identity = identity
        self._clock = clock

    @property
    def embedding(self) -> EmbeddingIdentity:
        return self._identity

    async def _lock_product(self, session: AsyncSession, product: AuthoritativeProduct) -> None:
        source = await session.scalar(
            select(SourceDocument)
            .where(
                SourceDocument.tenant_id == product.scope.tenant_id,
                SourceDocument.project_id == product.scope.project_id,
                SourceDocument.id == product.source_document_id,
            )
            .with_for_update()
        )
        if (
            source is None
            or source.status != "chunks_ready"
            or source.active_parse_run_id != product.parse_run_id
            or source.aggregate_version != product.product_version
        ):
            raise ProjectionCheckpointCASConflict()
        run = await session.scalar(
            select(SourceParseRun)
            .where(
                SourceParseRun.tenant_id == product.scope.tenant_id,
                SourceParseRun.project_id == product.scope.project_id,
                SourceParseRun.source_document_id == product.source_document_id,
                SourceParseRun.id == product.parse_run_id,
            )
            .with_for_update()
        )
        if (
            run is None
            or run.status != "succeeded"
            or run.product_version != product.product_version
            or run.product_sha256 != product.product_sha256
        ):
            raise ProjectionCheckpointCASConflict()

    async def _locked(
        self, session: AsyncSession, product: AuthoritativeProduct
    ) -> ProjectionCheckpoint | None:
        row = await session.scalar(
            select(ProjectionCheckpoint)
            .where(
                ProjectionCheckpoint.tenant_id == product.scope.tenant_id,
                ProjectionCheckpoint.project_id == product.scope.project_id,
                ProjectionCheckpoint.projection_name == "qdrant_source_chunks",
                ProjectionCheckpoint.checkpoint_key
                == SqlAlchemyProjectionCheckpoints._key(product, self._identity),
            )
            .with_for_update()
        )
        return row

    async def begin(self, product: AuthoritativeProduct) -> RebuildAttempt:
        async with self._session_factory() as session, session.begin():
            await self._lock_product(session, product)
            # Read immutable event provenance. Published/dead/leased events remain eligible.
            event = await session.scalar(
                select(OutboxEvent).where(
                    OutboxEvent.tenant_id == product.scope.tenant_id,
                    OutboxEvent.project_id == product.scope.project_id,
                    OutboxEvent.aggregate_type == "source",
                    OutboxEvent.aggregate_id == product.source_document_id,
                    OutboxEvent.aggregate_version == product.product_version,
                    OutboxEvent.event_type == "source.chunks.ready.v1",
                )
            )
            if (
                event is None
                or event.tenant_id != product.scope.tenant_id
                or event.project_id != product.scope.project_id
                or event.aggregate_type != "source"
                or event.aggregate_id != product.source_document_id
                or event.aggregate_version != product.product_version
                or event.event_type != "source.chunks.ready.v1"
            ):
                raise ProjectionCheckpointCASConflict()
            row = await self._locked(session, product)
            if row is not None and (
                row.projection_version > product.product_version
                or row.attempted_version > product.product_version
            ):
                raise ProjectionCheckpointCASConflict()
            if row is None:
                row = ProjectionCheckpoint(
                    tenant_id=product.scope.tenant_id,
                    project_id=product.scope.project_id,
                    projection_name="qdrant_source_chunks",
                    checkpoint_key=SqlAlchemyProjectionCheckpoints._key(product, self._identity),
                    projection_version=0,
                    attempted_version=0,
                    attempt_count=0,
                )
                session.add(row)
            attempt = RebuildAttempt(uuid.uuid4(), event.id, event.occurred_at)
            now = self._clock()
            row.status = "rebuilding"
            row.attempted_version = product.product_version
            row.last_attempted_event_id = attempt.event_id
            row.attempt_token = attempt.token
            row.failure_code = None
            row.source_document_id = product.source_document_id
            row.parse_run_id = product.parse_run_id
            row.product_sha256 = product.product_sha256
            row.embedding_identity_sha256 = embedding_identity_sha256(self._identity)
            row.vector_schema_version = VECTOR_SCHEMA_VERSION
            row.attempt_count += 1
            row.lag_started_at = row.lag_started_at or now
            row.updated_at = now
            await session.flush()
            return attempt

    async def _finish(
        self, product: AuthoritativeProduct, attempt: RebuildAttempt, code: str | None
    ) -> None:
        async with self._session_factory() as session, session.begin():
            await self._lock_product(session, product)
            row = await self._locked(session, product)
            if (
                row is None
                or row.attempt_token != attempt.token
                or row.last_attempted_event_id != attempt.event_id
                or row.attempted_version != product.product_version
                or row.projection_version > product.product_version
                or row.parse_run_id != product.parse_run_id
                or row.product_sha256 != product.product_sha256
                or row.status != "rebuilding"
            ):
                raise ProjectionCheckpointCASConflict()
            now = self._clock()
            row.status = "current" if code is None else "failed"
            row.failure_code = code
            row.attempt_token = None
            row.updated_at = now
            if code is None:
                row.projection_version = product.product_version
                row.last_applied_event_id = attempt.event_id
                row.last_applied_event_occurred_at = attempt.occurred_at
                row.lag_started_at = None
                row.last_succeeded_at = now
            await session.flush()

    async def succeed(self, product: AuthoritativeProduct, attempt: RebuildAttempt) -> None:
        await self._finish(product, attempt, None)

    async def fail(self, product: AuthoritativeProduct, attempt: RebuildAttempt, code: str) -> None:
        if code not in ProjectionWorkerError.CODES:
            raise ValueError("unknown projection failure code")
        await self._finish(product, attempt, code)
