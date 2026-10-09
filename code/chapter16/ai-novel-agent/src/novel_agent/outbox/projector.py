from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol, cast

from pydantic import ValidationError
from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novel_agent.db.models.outbox import OutboxEvent
from novel_agent.db.models.source import (
    ProjectionCheckpoint,
    SourceChunk,
    SourceDocument,
    SourceParseRun,
    SourceSection,
)
from novel_agent.embeddings.ports import (
    EmbeddingIdentity,
    EmbeddingProvider,
    EmbeddingProviderError,
    embed_bounded,
)
from novel_agent.sources.events import SourceChunksReadyV1
from novel_agent.vector.qdrant import ProjectionFailure
from novel_agent.vector.schema import (
    ChunkVectorPayload,
    TenantProjectScope,
    VectorPoint,
)

ProjectionResult = Literal["ignored", "stale", "applied"]


class ProjectionWorkerError(RuntimeError):
    """Explicit, sanitized worker failure safe for durable delivery records."""

    CODES = frozenset({
        "invalid_embedding", "embedding_unavailable", "invalid_source_chunks_event",
        "outbox_envelope_mismatch", "authoritative_product_mismatch",
        "authoritative_product_invalid", "projection_data_invalid", "projection_unavailable",
    })

    def __init__(self, code: str) -> None:
        if code not in self.CODES:
            raise ValueError("unknown projection failure code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class ClaimedOutboxEvent:
    id: uuid.UUID
    tenant_id: uuid.UUID
    project_id: uuid.UUID
    aggregate_id: uuid.UUID
    aggregate_version: int
    event_type: str
    occurred_at: datetime
    payload: dict[str, Any]
    lease_token: uuid.UUID | None = None
    lease_owner: str | None = None


@dataclass(frozen=True)
class AuthoritativeChunk:
    id: uuid.UUID
    section_id: uuid.UUID
    text: str
    heading: str | None
    source_ref_kind: Literal["paragraph", "table_row", "page"]
    source_ref_index: int
    source_ref_subindex: int | None
    char_start: int
    char_end: int


@dataclass(frozen=True)
class AuthoritativeProduct:
    scope: TenantProjectScope
    source_document_id: uuid.UUID
    parse_run_id: uuid.UUID
    product_version: int
    product_sha256: str
    source_label: str
    parser_name: str
    parser_version: str
    parser_config_sha256: str
    chunker_name: str
    chunker_version: str
    chunker_config_sha256: str
    containment_policy: Literal["single-section-v1"]
    chunks: tuple[AuthoritativeChunk, ...]


class ProjectionAuthority(Protocol):
    async def load_product(
        self, claimed: ClaimedOutboxEvent, parsed: SourceChunksReadyV1
    ) -> AuthoritativeProduct | None: ...


class VectorProjection(Protocol):
    embedding: EmbeddingIdentity

    async def upsert(self, scope: TenantProjectScope, points: tuple[VectorPoint, ...]) -> None: ...


class ProjectionCheckpoints(Protocol):
    async def begin(
        self,
        product: AuthoritativeProduct,
        event: ClaimedOutboxEvent,
        identity: EmbeddingIdentity,
    ) -> bool: ...

    async def succeed(
        self,
        product: AuthoritativeProduct,
        event: ClaimedOutboxEvent,
        identity: EmbeddingIdentity,
    ) -> None: ...

    async def fail(
        self,
        product: AuthoritativeProduct,
        event: ClaimedOutboxEvent,
        identity: EmbeddingIdentity,
        code: str,
        *,
        permanent: bool,
    ) -> None: ...


class ProjectionWorker:
    """Idempotent source-chunk projector; acknowledgement belongs to the runner."""

    def __init__(
        self,
        authority: ProjectionAuthority,
        embedding: EmbeddingProvider,
        projection: VectorProjection,
        checkpoints: ProjectionCheckpoints,
    ) -> None:
        if projection.embedding != embedding.identity:
            raise ValueError("projection and provider embedding identities differ")
        self._authority = authority
        self._embedding = embedding
        self._projection = projection
        self._checkpoints = checkpoints

    async def project(self, event: ClaimedOutboxEvent) -> ProjectionResult:
        if event.event_type != "source.chunks.ready.v1":
            return "ignored"
        try:
            parsed = SourceChunksReadyV1.model_validate(event.payload, strict=False)
        except ValidationError:
            raise ProjectionWorkerError("invalid_source_chunks_event") from None
        if (
            parsed.event_id != event.id
            or parsed.tenant_id != event.tenant_id
            or parsed.project_id != event.project_id
            or parsed.aggregate_id != event.aggregate_id
            or parsed.aggregate_version != event.aggregate_version
        ):
            raise ProjectionWorkerError("outbox_envelope_mismatch")
        product = await self._authority.load_product(event, parsed)
        if product is None:
            return "stale"
        self._validate_authority(product, parsed)
        if not await self._checkpoints.begin(product, event, self._embedding.identity):
            return "stale"
        try:
            await self._project_product(product)
        except EmbeddingProviderError as error:
            permanent = str(error) == "invalid embedding response"
            code = "invalid_embedding" if permanent else "embedding_unavailable"
            await self._checkpoints.fail(
                product, event, self._embedding.identity, code, permanent=permanent
            )
            raise ProjectionWorkerError(code) from None
        except (ValidationError, ValueError):
            await self._checkpoints.fail(
                product,
                event,
                self._embedding.identity,
                "projection_data_invalid",
                permanent=True,
            )
            raise ProjectionWorkerError("projection_data_invalid") from None
        except Exception:
            await self._checkpoints.fail(
                product,
                event,
                self._embedding.identity,
                "projection_unavailable",
                permanent=False,
            )
            raise ProjectionWorkerError("projection_unavailable") from None
        await self._checkpoints.succeed(product, event, self._embedding.identity)
        return "applied"

    @property
    def embedding(self) -> EmbeddingIdentity:
        return self._embedding.identity

    async def project_product(self, product: AuthoritativeProduct) -> None:
        """Shared bounded primitive for rebuilds; it does not mutate event checkpoints."""
        await self._project_product(product)

    async def _project_product(self, product: AuthoritativeProduct) -> None:
        size = min(self._embedding.limits.max_batch_size, 256)
        chunks = product.chunks
        for offset in range(0, len(chunks), size):
            batch = chunks[offset : offset + size]
            vectors = await embed_bounded(self._embedding, tuple(item.text for item in batch))
            points = tuple(
                VectorPoint.from_payload(
                    self._payload(product, chunk), vector, self._embedding.identity
                )
                for chunk, vector in zip(batch, vectors, strict=True)
            )
            await self._projection.upsert(product.scope, points)

    def _payload(
        self, product: AuthoritativeProduct, chunk: AuthoritativeChunk
    ) -> ChunkVectorPayload:
        identity = self._embedding.identity
        return ChunkVectorPayload(
            tenant_id=product.scope.tenant_id,
            project_id=product.scope.project_id,
            source_document_id=product.source_document_id,
            parse_run_id=product.parse_run_id,
            product_version=product.product_version,
            product_sha256=product.product_sha256,
            section_id=chunk.section_id,
            chunk_id=chunk.id,
            memory_kind="source_chunk",
            authority="postgresql",
            status="active",
            parser_name=product.parser_name,
            parser_version=product.parser_version,
            parser_config_sha256=product.parser_config_sha256,
            chunker_name=product.chunker_name,
            chunker_version=product.chunker_version,
            chunker_config_sha256=product.chunker_config_sha256,
            containment_policy=product.containment_policy,
            embedding_provider=identity.provider,
            embedding_model=identity.model,
            embedding_version=identity.version,
            embedding_dimension=identity.dimension,
            embedding_config_sha256=identity.configuration_sha256,
            embedding_distance=identity.distance,
            source_ref_kind=chunk.source_ref_kind,
            source_ref_index=chunk.source_ref_index,
            source_ref_subindex=chunk.source_ref_subindex,
            char_start=chunk.char_start,
            char_end=chunk.char_end,
            citation_label=product.source_label,
            citation_heading=chunk.heading,
            text=chunk.text,
        )

    @staticmethod
    def _validate_authority(product: AuthoritativeProduct, event: SourceChunksReadyV1) -> None:
        expected = (
            product.scope.tenant_id == event.tenant_id,
            product.scope.project_id == event.project_id,
            product.source_document_id == event.source_document_id,
            product.parse_run_id == event.parse_run_id,
            product.product_version == event.aggregate_version,
            product.product_sha256 == event.product_sha256,
            product.parser_name == event.parser_name,
            product.parser_version == event.parser_version,
            product.parser_config_sha256 == event.parser_config_sha256,
            product.chunker_name == event.chunker_name,
            product.chunker_version == event.chunker_version,
            product.chunker_config_sha256 == event.chunker_config_sha256,
            product.containment_policy == event.containment_policy,
            len(product.chunks) == event.chunk_count,
        )
        if not all(expected):
            raise ProjectionWorkerError("authoritative_product_mismatch")


def embedding_identity_sha256(identity: EmbeddingIdentity) -> str:
    material = json.dumps(
        identity.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(material).hexdigest()


class SqlAlchemyProjectionAuthority:
    """Loads the exact active product from PostgreSQL; event payload is never authority."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def load_product(
        self, claimed: ClaimedOutboxEvent, parsed: SourceChunksReadyV1
    ) -> AuthoritativeProduct | None:
        async with self._session_factory() as session, session.begin():
            return await self._load_product(session, claimed, parsed)

    async def _load_product(
        self, session: AsyncSession, claimed: ClaimedOutboxEvent, parsed: SourceChunksReadyV1
    ) -> AuthoritativeProduct | None:
        header = (
            await session.execute(
                select(SourceDocument, SourceParseRun)
                .join(
                    SourceParseRun,
                    and_(
                        SourceParseRun.tenant_id == SourceDocument.tenant_id,
                        SourceParseRun.project_id == SourceDocument.project_id,
                        SourceParseRun.source_document_id == SourceDocument.id,
                        SourceParseRun.id == SourceDocument.active_parse_run_id,
                    ),
                )
                .where(
                    SourceDocument.tenant_id == claimed.tenant_id,
                    SourceDocument.project_id == claimed.project_id,
                    SourceDocument.id == claimed.aggregate_id,
                    SourceDocument.status == "chunks_ready",
                    SourceDocument.aggregate_version == claimed.aggregate_version,
                    SourceParseRun.id == parsed.parse_run_id,
                    SourceParseRun.status == "succeeded",
                    SourceParseRun.product_version == claimed.aggregate_version,
                    SourceParseRun.product_sha256 == parsed.product_sha256,
                )
            )
        ).one_or_none()
        if header is None:
            return None
        source, run = header
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
                    SourceChunk.tenant_id == claimed.tenant_id,
                    SourceChunk.project_id == claimed.project_id,
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
        if not chunks or any(
            item.source_ref_kind is None or item.source_ref_index is None for item in chunks
        ):
            raise ProjectionWorkerError("authoritative_product_invalid")
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


class ProjectionLeaseLost(RuntimeError):
    def __init__(self) -> None:
        super().__init__("projection_lease_lost")


class ProjectionCheckpointCASConflict(RuntimeError):
    def __init__(self) -> None:
        super().__init__("projection_checkpoint_cas_conflict")


class SqlAlchemyProjectionCheckpoints:
    """Each checkpoint transition owns a short transaction, fenced by the delivery lease."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        self._session_factory = session_factory
        self._clock = clock

    @staticmethod
    def _key(product: AuthoritativeProduct, identity: EmbeddingIdentity) -> str:
        identity_hash = embedding_identity_sha256(identity)
        return f"source:{product.source_document_id}:embedding:{identity_hash}"

    async def begin(
        self, product: AuthoritativeProduct, event: ClaimedOutboxEvent, identity: EmbeddingIdentity
    ) -> bool:
        async with self._session_factory() as session, session.begin():
            lease = await self._lock_authority(session, product, event)
            row = await self._locked(session, product, identity)
            now = self._clock()
            self._validate_lease(lease, event, now)
            if row is not None and row.projection_version >= event.aggregate_version:
                return False
            if row is not None and row.attempted_version > event.aggregate_version:
                raise ProjectionCheckpointCASConflict()
            if row is None:
                # The source row lock serializes creators for every embedding key of this source.
                row = ProjectionCheckpoint(
                    tenant_id=product.scope.tenant_id,
                    project_id=product.scope.project_id,
                    projection_name="qdrant_source_chunks",
                    checkpoint_key=self._key(product, identity),
                    projection_version=0,
                    attempted_version=0,
                    attempt_count=0,
                )
                session.add(row)
            row.status = "rebuilding"
            row.attempted_version = event.aggregate_version
            row.last_attempted_event_id = event.id
            row.attempt_token = event.lease_token
            row.failure_code = None
            row.source_document_id = product.source_document_id
            row.parse_run_id = product.parse_run_id
            row.product_sha256 = product.product_sha256
            row.embedding_identity_sha256 = embedding_identity_sha256(identity)
            row.vector_schema_version = 1
            row.attempt_count += 1
            row.lag_started_at = row.lag_started_at or now
            row.updated_at = now
            await session.flush()
            return True

    async def succeed(
        self, product: AuthoritativeProduct, event: ClaimedOutboxEvent, identity: EmbeddingIdentity
    ) -> None:
        async with self._session_factory() as session, session.begin():
            lease = await self._lock_authority(session, product, event)
            row = await self._locked(session, product, identity)
            now = self._clock()
            self._validate_lease(lease, event, now)
            self._validate_attempt(row, event)
            assert row is not None
            row.projection_version = event.aggregate_version
            row.last_applied_event_id = event.id
            row.last_applied_event_occurred_at = event.occurred_at
            row.status = "current"
            row.failure_code = None
            row.attempt_token = None
            row.lag_started_at = None
            row.last_succeeded_at = now
            row.updated_at = now
            await session.flush()

    async def fail(
        self,
        product: AuthoritativeProduct,
        event: ClaimedOutboxEvent,
        identity: EmbeddingIdentity,
        code: str,
        *,
        permanent: bool,
    ) -> None:
        if code not in ProjectionWorkerError.CODES:
            raise ValueError("unknown projection failure code")
        async with self._session_factory() as session, session.begin():
            lease = await self._lock_authority(session, product, event)
            row = await self._locked(session, product, identity)
            now = self._clock()
            self._validate_lease(lease, event, now)
            self._validate_attempt(row, event)
            assert row is not None
            row.status = "failed" if permanent else "lagging"
            row.failure_code = code if permanent else None
            row.attempt_token = None
            row.updated_at = now
            await session.flush()

    @staticmethod
    def _validate_attempt(row: ProjectionCheckpoint | None, event: ClaimedOutboxEvent) -> None:
        if (
            row is None
            or row.attempt_token != event.lease_token
            or row.attempted_version != event.aggregate_version
            or row.last_attempted_event_id != event.id
            or row.projection_version >= event.aggregate_version
        ):
            raise ProjectionCheckpointCASConflict()

    @staticmethod
    def _validate_lease(row: OutboxEvent | None, event: ClaimedOutboxEvent, now: datetime) -> None:
        if (
            row is None or event.lease_token is None or not event.lease_owner
            or row.id != event.id or row.lease_token != event.lease_token
            or row.lease_owner != event.lease_owner
            or row.tenant_id != event.tenant_id or row.project_id != event.project_id
            or row.aggregate_id != event.aggregate_id
            or row.aggregate_version != event.aggregate_version
            or row.event_type != event.event_type
            or row.published_at is not None or row.dead_at is not None
            or row.lease_expires_at is None or row.lease_expires_at <= now
        ):
            raise ProjectionLeaseLost()

    async def _lock_authority(
        self, session: AsyncSession, product: AuthoritativeProduct, event: ClaimedOutboxEvent
    ) -> OutboxEvent:
        # Consistent order: delivery, source, parse run, checkpoint. No external I/O here.
        lease = await session.scalar(select(OutboxEvent).where(
            OutboxEvent.id == event.id,
            OutboxEvent.tenant_id == event.tenant_id,
            OutboxEvent.project_id == event.project_id,
        ).with_for_update())
        self._validate_lease(lease, event, self._clock())
        if (
            product.scope.tenant_id != event.tenant_id
            or product.scope.project_id != event.project_id
            or product.source_document_id != event.aggregate_id
            or product.product_version != event.aggregate_version
        ):
            raise ProjectionCheckpointCASConflict()
        source = await session.scalar(select(SourceDocument).where(
            SourceDocument.tenant_id == event.tenant_id,
            SourceDocument.project_id == event.project_id,
            SourceDocument.id == event.aggregate_id,
        ).with_for_update())
        if (
            source is None or source.status != "chunks_ready"
            or source.active_parse_run_id != product.parse_run_id
            or source.aggregate_version != event.aggregate_version
        ):
            raise ProjectionCheckpointCASConflict()
        run = await session.scalar(select(SourceParseRun).where(
            SourceParseRun.tenant_id == event.tenant_id,
            SourceParseRun.project_id == event.project_id,
            SourceParseRun.source_document_id == event.aggregate_id,
            SourceParseRun.id == product.parse_run_id,
        ).with_for_update())
        if (
            run is None or run.status != "succeeded"
            or run.product_version != event.aggregate_version
            or run.product_sha256 != product.product_sha256
        ):
            raise ProjectionCheckpointCASConflict()
        assert lease is not None
        return lease

    async def _locked(
        self, session: AsyncSession, product: AuthoritativeProduct, identity: EmbeddingIdentity
    ) -> ProjectionCheckpoint | None:
        return cast(
            ProjectionCheckpoint | None,
            await session.scalar(
                select(ProjectionCheckpoint)
                .where(
                    ProjectionCheckpoint.tenant_id == product.scope.tenant_id,
                    ProjectionCheckpoint.project_id == product.scope.project_id,
                    ProjectionCheckpoint.projection_name == "qdrant_source_chunks",
                    ProjectionCheckpoint.checkpoint_key == self._key(product, identity),
                )
                .with_for_update()
            ),
        )


class SqlAlchemyOutboxLeases:
    """Short PG leases fenced by token, owner, expiry, and terminal state."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    @staticmethod
    def claim_statement(*, now: datetime) -> Any:
        return (
            select(OutboxEvent)
            .where(
                OutboxEvent.event_type == "source.chunks.ready.v1",
                OutboxEvent.published_at.is_(None),
                OutboxEvent.dead_at.is_(None),
                or_(OutboxEvent.next_attempt_at.is_(None), OutboxEvent.next_attempt_at <= now),
                or_(OutboxEvent.lease_expires_at.is_(None), OutboxEvent.lease_expires_at <= now),
            )
            .order_by(OutboxEvent.occurred_at, OutboxEvent.id)
            .limit(1)
            .with_for_update(skip_locked=True)
        )

    async def claim(
        self, *, owner: str, now: datetime, lease_for: timedelta
    ) -> ClaimedOutboxEvent | None:
        self._validate_owner(owner)
        if lease_for <= timedelta(0):
            raise ValueError("positive lease duration is required")
        row = await self._session.scalar(self.claim_statement(now=now))
        if row is None:
            return None
        token = uuid.uuid4()
        row.lease_token = token
        row.lease_owner = owner
        row.lease_expires_at = now + lease_for
        await self._session.flush()
        return ClaimedOutboxEvent(
            id=row.id,
            tenant_id=row.tenant_id,
            project_id=row.project_id,
            aggregate_id=row.aggregate_id,
            aggregate_version=row.aggregate_version,
            event_type=row.event_type,
            occurred_at=row.occurred_at,
            payload=row.payload,
            lease_token=token,
            lease_owner=owner,
        )

    @staticmethod
    def _validate_owner(owner: str) -> None:
        if not isinstance(owner, str) or not owner.strip() or len(owner) > 100:
            raise ValueError("valid lease owner is required")

    @classmethod
    def _fence(cls, event_id: uuid.UUID, token: uuid.UUID, owner: str, now: datetime) -> Any:
        cls._validate_owner(owner)
        return and_(
            OutboxEvent.id == event_id,
            OutboxEvent.lease_token == token,
            OutboxEvent.lease_owner == owner,
            OutboxEvent.published_at.is_(None),
            OutboxEvent.dead_at.is_(None),
            OutboxEvent.lease_expires_at > now,
        )

    async def acknowledge(
        self, event_id: uuid.UUID, token: uuid.UUID, *, owner: str, now: datetime
    ) -> bool:
        result = await self._session.execute(
            update(OutboxEvent)
            .where(self._fence(event_id, token, owner, now))
            .values(
                published_at=now,
                lease_token=None,
                lease_owner=None,
                lease_expires_at=None,
                last_error=None,
            )
        )
        return bool(cast(Any, result).rowcount)

    async def release(
        self,
        event_id: uuid.UUID,
        token: uuid.UUID,
        *,
        owner: str,
        now: datetime,
        error_code: str,
        max_attempts: int = 8,
    ) -> bool:
        if error_code not in ProjectionWorkerError.CODES:
            raise ValueError("unknown projection failure code")
        if max_attempts <= 0:
            raise ValueError("positive retry attempt limit is required")
        row = await self._session.scalar(
            select(OutboxEvent)
            .where(self._fence(event_id, token, owner, now))
            .with_for_update()
        )
        if row is None:
            return False
        row.retry_count += 1
        row.last_error = error_code
        row.lease_token = None
        row.lease_owner = None
        row.lease_expires_at = None
        if row.retry_count >= max_attempts:
            row.dead_at = now
            row.dead_reason = "retry_exhausted"
        else:
            seconds = min(3600, 2 ** min(row.retry_count, 10))
            row.next_attempt_at = now + timedelta(seconds=seconds)
        await self._session.flush()
        return True

    async def dead_letter(
        self,
        event_id: uuid.UUID,
        token: uuid.UUID,
        *,
        owner: str,
        now: datetime,
        error_code: str,
    ) -> bool:
        if error_code not in ProjectionWorkerError.CODES:
            raise ValueError("unknown projection failure code")
        result = await self._session.execute(
            update(OutboxEvent)
            .where(self._fence(event_id, token, owner, now))
            .values(
                dead_at=now,
                dead_reason=error_code,
                last_error=error_code,
                lease_token=None,
                lease_owner=None,
                lease_expires_at=None,
            )
        )
        return bool(cast(Any, result).rowcount)


class OutboxProjectionRunner:
    """Production composition with a short committed lease and durable failure recording."""

    _PERMANENT = frozenset(
        {
            "invalid_embedding",
            "invalid_source_chunks_event",
            "outbox_envelope_mismatch",
            "authoritative_product_mismatch",
            "authoritative_product_invalid",
            "projection_data_invalid",
        }
    )

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        worker_factory: Callable[[], ProjectionWorker],
        *,
        owner: str,
        lease_for: timedelta = timedelta(minutes=5),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        SqlAlchemyOutboxLeases._validate_owner(owner)
        if lease_for <= timedelta(0):
            raise ValueError("valid projection runner lease settings are required")
        self._session_factory = session_factory
        self._worker_factory = worker_factory
        self._owner = owner
        self._lease_for = lease_for
        self._clock = clock

    async def run_once(self, *, now: datetime | None = None) -> bool:
        # An explicit now only overrides claim time; completion always reads the clock.
        claim_time = now or self._clock()
        async with self._session_factory() as session, session.begin():
            claimed = await SqlAlchemyOutboxLeases(session).claim(
                owner=self._owner, now=claim_time, lease_for=self._lease_for
            )
        if claimed is None:
            return False
        token = claimed.lease_token
        if token is None:
            raise RuntimeError("claimed_event_missing_lease_token")
        error_code: str | None = None
        try:
            await self._worker_factory().project(claimed)
        except ProjectionLeaseLost:
            raise
        except ProjectionWorkerError as error:
            error_code = error.code
        except Exception:
            error_code = "projection_unavailable"
        async with self._session_factory() as session, session.begin():
            leases = SqlAlchemyOutboxLeases(session)
            finish_time = self._clock()
            if error_code is None:
                changed = await leases.acknowledge(
                    claimed.id, token, owner=self._owner, now=finish_time
                )
            elif error_code in self._PERMANENT:
                changed = await leases.dead_letter(
                    claimed.id, token, owner=self._owner, now=finish_time, error_code=error_code
                )
            else:
                changed = await leases.release(
                    claimed.id, token, owner=self._owner, now=finish_time, error_code=error_code
                )
        if not changed:
            raise ProjectionLeaseLost()
        return True


class SqlAlchemyProjectionLagRecorder:
    """Task 9's mandatory recorder backed by an independent PostgreSQL transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def record(self, failure: ProjectionFailure) -> None:
        if failure.tenant_id is None or failure.project_id is None:
            # The tenant-scoped checkpoint table cannot truthfully represent a global setup error.
            return
        now = datetime.now(UTC)
        key = f"transport:{failure.operation}:{failure.collection}"[:200]
        statement = insert(ProjectionCheckpoint).values(
            id=uuid.uuid4(),
            tenant_id=failure.tenant_id,
            project_id=failure.project_id,
            projection_name="qdrant_transport",
            checkpoint_key=key,
            projection_version=0,
            status="failed",
            attempted_version=0,
            failure_code=failure.error_code,
            attempt_count=1,
            lag_started_at=now,
            updated_at=now,
        )
        statement = statement.on_conflict_do_update(
            constraint="uq_projection_ckpts_scope_name_key",
            set_={
                "status": "failed",
                "failure_code": statement.excluded.failure_code,
                "attempt_count": ProjectionCheckpoint.attempt_count + 1,
                "lag_started_at": func.coalesce(
                    ProjectionCheckpoint.lag_started_at, statement.excluded.lag_started_at
                ),
                "updated_at": statement.excluded.updated_at,
            },
        )
        async with self._session_factory() as session, session.begin():
            await session.execute(statement)


__all__ = [
    "AuthoritativeChunk",
    "AuthoritativeProduct",
    "ClaimedOutboxEvent",
    "ProjectionWorker",
    "ProjectionWorkerError",
    "ProjectionLeaseLost",
    "ProjectionCheckpointCASConflict",
    "OutboxProjectionRunner",
    "SqlAlchemyOutboxLeases",
    "SqlAlchemyProjectionAuthority",
    "SqlAlchemyProjectionCheckpoints",
    "SqlAlchemyProjectionLagRecorder",
    "embedding_identity_sha256",
]
