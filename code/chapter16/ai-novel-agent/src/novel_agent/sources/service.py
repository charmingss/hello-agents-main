from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Protocol, runtime_checkable

from sqlalchemy.ext.asyncio import AsyncSession

from novel_agent.chunking.novel import canonical_product_sha256
from novel_agent.db.models.source import SourceDocument, SourceParseRun
from novel_agent.outbox.events import IntegrationEvent
from novel_agent.outbox.repository import SqlAlchemyOutboxRepository
from novel_agent.sources.contracts import ParserProfile, RequestId, SourceScope
from novel_agent.sources.events import (
    FailedStage,
    SourceAcceptedV1,
    SourceChunksReadyV1,
    SourceFailedV1,
)
from novel_agent.sources.repository import (
    CreateResult,
    NewAcceptedSource,
    NewParseRun,
    PersistParseProduct,
    PersistProductResult,
    SqlAlchemySourceRepository,
)

_CHUNKS_READY_EVENT_NAMESPACE = uuid.UUID("7c5ea495-0663-4db4-a760-6c514f90fbea")


class SourceRepository(Protocol):
    async def create_accepted(self, value: NewAcceptedSource) -> CreateResult[SourceDocument]: ...

    async def create_parse_run(self, value: NewParseRun) -> CreateResult[SourceParseRun]: ...

    async def begin_parse(self, value: NewParseRun) -> CreateResult[SourceParseRun]: ...

    async def persist_parse_product(self, value: PersistParseProduct) -> PersistProductResult: ...

    async def transition_source(
        self,
        scope: SourceScope,
        source_document_id: uuid.UUID,
        expected_status: str,
        target_status: str,
        *,
        failure_code: str | None = None,
        parse_run_id: uuid.UUID | None = None,
        source_content_sha256: str | None = None,
        profile: ParserProfile | None = None,
        required_parse_status: str | None = None,
    ) -> CreateResult[SourceDocument]: ...

    async def transition_parse_run(
        self,
        scope: SourceScope,
        parse_run_id: uuid.UUID,
        expected_status: str,
        target_status: str,
        *,
        source_document_id: uuid.UUID,
        source_content_sha256: str,
        profile: ParserProfile,
        failure_code: str | None = None,
        occurred_at: datetime,
    ) -> CreateResult[SourceParseRun]: ...


class SourceOutboxRepository(Protocol):
    async def add(self, event: IntegrationEvent) -> object: ...


@runtime_checkable
class SourceUnitOfWork(Protocol):
    sources: SourceRepository
    outbox: SourceOutboxRepository


class SqlAlchemySourceUnitOfWork:
    """Creates both repositories from exactly one caller-owned SQLAlchemy session."""

    def __init__(self, session: AsyncSession) -> None:
        self.sources: SourceRepository = SqlAlchemySourceRepository(session)
        self.outbox: SourceOutboxRepository = SqlAlchemyOutboxRepository(session)


class SourceApplicationService:
    """Coordinates authority writes without owning or committing the SQL transaction."""

    def __init__(self, unit_of_work: SourceUnitOfWork) -> None:
        self._sources = unit_of_work.sources
        self._outbox = unit_of_work.outbox

    async def accept_source(self, value: NewAcceptedSource) -> SourceDocument:
        result = await self._sources.create_accepted(value)
        await self._outbox.add(
            SourceAcceptedV1(
                event_id=value.event_id,
                tenant_id=value.scope.tenant_id,
                project_id=value.scope.project_id,
                aggregate_id=result.value.id,
                source_document_id=result.value.id,
                occurred_at=value.accepted_at,
                request_id=value.upload_request_id,
                correlation_id=value.correlation_id,
                causation_id=value.causation_id,
                content_sha256=value.content_sha256,
                media_type=value.detected_media_type,
                byte_count=value.byte_count,
            )
        )
        return result.value

    async def begin_parse(self, value: NewParseRun) -> SourceParseRun:
        return (await self._sources.begin_parse(value)).value

    async def persist_parse_product(self, value: PersistParseProduct) -> SourceDocument:
        result = await self._sources.persist_parse_product(value)
        product = value.product
        if not result.created:
            return result.source
        event_id = uuid.uuid5(
            _CHUNKS_READY_EVENT_NAMESPACE,
            f"{value.scope.tenant_id}:{value.scope.project_id}:{value.parse_run_id}",
        )
        await self._outbox.add(
            SourceChunksReadyV1(
                event_id=event_id,
                tenant_id=value.scope.tenant_id,
                project_id=value.scope.project_id,
                aggregate_id=value.source_document_id,
                source_document_id=value.source_document_id,
                aggregate_version=result.product_version,
                occurred_at=value.occurred_at,
                request_id=value.request_id,
                correlation_id=value.correlation_id,
                causation_id=value.causation_id,
                parse_run_id=value.parse_run_id,
                parser_name=product.parser_name,
                parser_version=product.parser_version,
                parser_config_sha256=product.parser_config_sha256,
                chunker_name=product.identity.name,
                chunker_version=product.identity.version,
                chunker_config_sha256=product.identity.configuration_sha256,
                product_sha256=canonical_product_sha256(product),
                containment_policy=product.identity.containment_policy,
                section_count=len(product.sections),
                chunk_count=len(product.chunks),
            )
        )
        return result.source

    async def fail(
        self,
        *,
        scope: SourceScope,
        source: SourceDocument,
        parse_run: SourceParseRun | None,
        profile: ParserProfile | None,
        failure_code: str,
        failed_stage: FailedStage,
        occurred_at: datetime,
        event_id: uuid.UUID,
        request_id: RequestId,
        correlation_id: uuid.UUID,
        causation_id: uuid.UUID | None,
    ) -> SourceDocument:
        if parse_run is not None and profile is None:
            raise ValueError("profile is required when a parse run is supplied")
        required_parse_status: str | None = None
        if parse_run is not None:
            assert profile is not None
            if parse_run.status in {"pending", "running", "failed"}:
                await self._sources.transition_parse_run(
                    scope,
                    parse_run.id,
                    parse_run.status,
                    "failed",
                    source_document_id=source.id,
                    source_content_sha256=source.content_sha256,
                    profile=profile,
                    failure_code=failure_code,
                    occurred_at=occurred_at,
                )
                required_parse_status = "failed"
            elif parse_run.status == "succeeded":
                required_parse_status = "succeeded"
            else:
                required_parse_status = parse_run.status
        source_result = await self._sources.transition_source(
            scope,
            source.id,
            source.status,
            "failed",
            failure_code=failure_code,
            parse_run_id=parse_run.id if parse_run is not None else None,
            source_content_sha256=source.content_sha256 if parse_run is not None else None,
            profile=profile,
            required_parse_status=required_parse_status,
        )
        failure_version: Literal[2, 3]
        if source_result.value.aggregate_version == 2:
            failure_version = 2
        elif source_result.value.aggregate_version == 3:
            failure_version = 3
        else:
            raise ValueError("failed source must have aggregate version 2 or 3")
        await self._outbox.add(
            SourceFailedV1(
                event_id=event_id,
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                aggregate_id=source.id,
                source_document_id=source.id,
                aggregate_version=failure_version,
                occurred_at=occurred_at,
                request_id=request_id,
                correlation_id=correlation_id,
                causation_id=causation_id,
                parse_run_id=parse_run.id if parse_run is not None else None,
                failure_code=failure_code,
                failed_stage=failed_stage,
            )
        )
        return source_result.value


__all__ = [
    "SourceApplicationService",
    "SourceUnitOfWork",
    "SqlAlchemySourceUnitOfWork",
]
