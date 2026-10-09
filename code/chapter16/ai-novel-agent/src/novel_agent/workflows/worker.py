from __future__ import annotations

import asyncio
import hashlib
import signal
import tempfile
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from types import FrameType
from typing import BinaryIO, Protocol, cast

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.worker import Worker

from novel_agent.analysis.deepseek import DeepSeekAnalysisProvider
from novel_agent.analysis.jobs import FullAnalysisJobService
from novel_agent.chunking.novel import ChunkedDocument, NovelChunker
from novel_agent.config import Settings, get_settings
from novel_agent.db.models.source import SourceDocument, SourceParseRun
from novel_agent.db.session import create_engine, create_session_factory
from novel_agent.outbox.repository import SqlAlchemyOutboxRepository
from novel_agent.parsing.contracts import (
    DocumentParser,
    ParserCancelled,
    ParseRequest,
    ParserError,
)
from novel_agent.parsing.docx import StdlibDocxParser
from novel_agent.parsing.pdf import PdfTextParser
from novel_agent.parsing.router import ParserRouter
from novel_agent.parsing.txt import StdlibTxtParser
from novel_agent.projects.repository import SqlAlchemyProjectRepository
from novel_agent.projects.schemas import CreateProject
from novel_agent.projects.service import OutboxRepository, ProjectRepository, ProjectService
from novel_agent.sources.contracts import ParserProfile, SourceScope
from novel_agent.sources.errors import SourceConflictError
from novel_agent.sources.events import FailedStage
from novel_agent.sources.repository import NewParseRun, PersistParseProduct
from novel_agent.sources.service import SourceApplicationService, SqlAlchemySourceUnitOfWork
from novel_agent.storage.keys import ImmutableObjectKey, StorageScope
from novel_agent.storage.minio import MinioObjectStore
from novel_agent.storage.ports import (
    ContentHashMismatch,
    ContentSizeMismatch,
    InvalidMediaSignature,
    ObjectChanged,
    ObjectMetadata,
    ObjectNotFound,
    ObjectReader,
    ObjectStorageError,
    ScopeViolation,
)
from novel_agent.workflows.full_analysis import (
    FullAnalysisActivities,
    FullAnalysisWorkflow,
)
from novel_agent.workflows.project_workflow import (
    InitializeProjectInput,
    InitializeProjectOutput,
    InitializeProjectWorkflow,
)
from novel_agent.workflows.source_ingestion import (
    SourceIngestionFailure,
    SourceIngestionInput,
    SourceIngestionOutput,
    SourceIngestionWorkflow,
    permanent_error_code,
)

GRACEFUL_SHUTDOWN_TIMEOUT = timedelta(seconds=30)


class _NullFullAnalysisGateway:
    """No-op gateway; the worker-side service never dispatches workflows."""

    async def dispatch(self, command: object, *, workflow_id: str) -> None:
        del command, workflow_id


ProjectRepositoryFactory = Callable[[AsyncSession], ProjectRepository]
OutboxRepositoryFactory = Callable[[AsyncSession], OutboxRepository]
ServiceFactory = Callable[[ProjectRepository, OutboxRepository], ProjectService]


class AsyncWorkerContext(Protocol):
    async def __aenter__(self) -> object: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: object | None,
    ) -> None: ...


class SourceIngestionAuthority(Protocol):
    async def begin(self, command: SourceIngestionInput, occurred_at: datetime) -> None: ...

    async def verify(self, command: SourceIngestionInput) -> None: ...

    async def persist(
        self,
        command: SourceIngestionInput,
        product: ChunkedDocument,
        occurred_at: datetime,
    ) -> SourceIngestionOutput: ...

    async def record_failure(
        self,
        command: SourceIngestionInput,
        error_code: str,
        stage: str,
        occurred_at: datetime,
    ) -> None: ...


_PERMANENT_STORAGE_ERRORS: tuple[type[BaseException], ...] = (
    ContentHashMismatch,
    ContentSizeMismatch,
    InvalidMediaSignature,
    ObjectChanged,
    ObjectNotFound,
    ScopeViolation,
)


def _safe_activity_heartbeat(stage: str) -> None:
    try:
        activity.heartbeat(stage)
    except RuntimeError:
        # Unit tests invoke activities without a Temporal activity context.
        return


def _activity_cancelled() -> bool:
    try:
        return activity.is_cancelled()
    except RuntimeError:
        return False


class SourceIngestionActivities:
    """Keeps large parse products in one activity process, never Temporal history."""

    def __init__(
        self,
        *,
        authority: SourceIngestionAuthority,
        object_reader: ObjectReader,
        parser_router: ParserRouter,
        cancellation_probe: Callable[[], bool] | None = None,
        heartbeat: Callable[[str], None] | None = None,
    ) -> None:
        self._authority = authority
        self._objects = object_reader
        self._parsers = parser_router
        self._cancellation_probe = cancellation_probe or _activity_cancelled
        self._heartbeat = heartbeat or _safe_activity_heartbeat

    def _ensure_not_cancelled(self) -> None:
        self._heartbeat("cancellation-check")
        if self._cancellation_probe():
            raise asyncio.CancelledError

    @activity.defn(name="begin_source_ingestion_activity")
    async def begin_source_ingestion_activity(self, command: SourceIngestionInput) -> None:
        try:
            self._ensure_not_cancelled()
            await self._authority.begin(command, datetime.now(UTC))
        except asyncio.CancelledError:
            raise
        except SourceConflictError:
            raise ApplicationError(
                "source ingestion authority conflicts with the request",
                type="authority_conflict",
                non_retryable=True,
            ) from None
        except SQLAlchemyError:
            raise ApplicationError(
                "source database is temporarily unavailable",
                type="database_unavailable",
            ) from None
        except Exception:
            raise ApplicationError(
                "source ingestion temporarily failed", type="ingestion_transient"
            ) from None

    def _read_verified(self, command: SourceIngestionInput) -> BinaryIO:
        scope = StorageScope(tenant_id=command.tenant_id, project_id=command.project_id)
        key = ImmutableObjectKey(value=command.object_key)
        metadata = self._objects.stat(scope, key)
        self._verify_metadata(command, metadata, key)
        spool = cast(
            BinaryIO,
            tempfile.SpooledTemporaryFile(max_size=min(command.byte_size, 8 * 1024**2)),
        )
        digest = hashlib.sha256()
        observed = 0
        try:
            for chunk in self._objects.read_bounded(
                scope, key, max_bytes=command.byte_size, chunk_size=64 * 1024
            ):
                if self._cancellation_probe():
                    raise ParserCancelled("source ingestion was canceled")
                if not isinstance(chunk, bytes) or not chunk:
                    raise ContentSizeMismatch("object reader returned an invalid chunk")
                observed += len(chunk)
                if observed > command.byte_size:
                    raise ContentSizeMismatch("object exceeded immutable size")
                digest.update(chunk)
                spool.write(chunk)
                self._heartbeat("read")
            if observed != command.byte_size:
                raise ContentSizeMismatch("object length changed")
            if digest.hexdigest() != command.content_sha256:
                raise ContentHashMismatch("object content changed")
            spool.seek(0)
            return spool
        except BaseException:
            spool.close()
            raise

    @staticmethod
    def _verify_metadata(
        command: SourceIngestionInput,
        metadata: ObjectMetadata,
        key: ImmutableObjectKey,
    ) -> None:
        if metadata.key.value != key.value:
            raise ScopeViolation("object metadata key changed")
        if metadata.byte_size != command.byte_size:
            raise ContentSizeMismatch("object size changed")
        if metadata.etag != command.object_etag:
            raise ObjectChanged("object etag changed")
        if metadata.version_id != command.object_version_id:
            raise ObjectChanged("object version changed")
        if metadata.stored_sha256 not in {None, command.content_sha256}:
            raise ContentHashMismatch("stored object hash changed")
        if metadata.stored_media_type not in {None, command.media_type}:
            raise InvalidMediaSignature("stored object media type changed")

    def _parse_and_chunk(self, command: SourceIngestionInput, stream: BinaryIO) -> ChunkedDocument:
        request = ParseRequest(
            source_id=command.source_document_id,
            file_type=command.file_type,
            media_type=command.media_type,
            byte_size=command.byte_size,
            content_sha256=command.content_sha256,
            budgets=command.parser_budgets,
        )
        parser = self._parsers.select(request)
        identity = parser.identity
        if (
            identity.name,
            identity.version,
            identity.configuration_sha256,
        ) != (
            command.parser_name,
            command.parser_version,
            command.parser_configuration_sha256,
        ):
            raise ApplicationError(
                "source parser identity does not match the requested profile",
                type="parser_identity_conflict",
                non_retryable=True,
            )
        parsed = parser.parse(request, stream, cancellation=self._cancellation_probe)
        self._heartbeat("chunk")
        chunker = NovelChunker(
            version=command.chunker_version,
            config=command.chunker_config,
        )
        if (
            chunker.identity.name,
            chunker.identity.configuration_sha256,
        ) != (command.chunker_name, command.chunker_configuration_sha256):
            raise ApplicationError(
                "source chunker identity does not match the requested profile",
                type="chunker_identity_conflict",
                non_retryable=True,
            )
        return chunker.chunk(
            parsed,
            source_document_id=command.source_document_id,
            source_content_sha256=command.content_sha256,
        )

    @activity.defn(name="process_source_ingestion_activity")
    async def process_source_ingestion_activity(
        self, command: SourceIngestionInput
    ) -> SourceIngestionOutput:
        stream: BinaryIO | None = None
        try:
            self._ensure_not_cancelled()
            await self._authority.verify(command)
            stream = await asyncio.to_thread(self._read_verified, command)
            product = await asyncio.to_thread(self._parse_and_chunk, command, stream)
            self._ensure_not_cancelled()
            await self._authority.verify(command)
            self._ensure_not_cancelled()
            return await self._authority.persist(command, product, datetime.now(UTC))
        except asyncio.CancelledError:
            raise
        except ApplicationError:
            raise
        except SourceConflictError:
            raise ApplicationError(
                "source ingestion authority conflicts with the request",
                type="authority_conflict",
                non_retryable=True,
            ) from None
        except ParserError as exc:
            code = permanent_error_code(exc)
            if isinstance(exc, ParserCancelled) and self._cancellation_probe():
                raise asyncio.CancelledError from None
            if code is None:
                raise ApplicationError(
                    "source ingestion temporarily failed", type="ingestion_transient"
                ) from None
            raise ApplicationError(
                "source content cannot be ingested",
                type=code,
                non_retryable=True,
            ) from None
        except _PERMANENT_STORAGE_ERRORS:
            raise ApplicationError(
                "immutable source verification failed",
                type="immutable_object_conflict",
                non_retryable=True,
            ) from None
        except ObjectStorageError:
            raise ApplicationError(
                "source storage is temporarily unavailable", type="storage_unavailable"
            ) from None
        except SQLAlchemyError:
            raise ApplicationError(
                "source database is temporarily unavailable", type="database_unavailable"
            ) from None
        except Exception:
            raise ApplicationError(
                "source ingestion temporarily failed", type="ingestion_transient"
            ) from None
        finally:
            if stream is not None:
                await asyncio.to_thread(stream.close)

    @activity.defn(name="record_source_ingestion_failure_activity")
    async def record_source_ingestion_failure_activity(
        self, failure: SourceIngestionFailure
    ) -> None:
        await self._authority.record_failure(
            failure.command, failure.error_code, failure.stage, datetime.now(UTC)
        )


def _profile(command: SourceIngestionInput) -> ParserProfile:
    return ParserProfile(
        parser_name=command.parser_name,
        parser_version=command.parser_version,
        chunker_version=command.chunker_version,
        configuration_sha256=command.parser_configuration_sha256,
        chunker_name=command.chunker_name,
        chunker_configuration_sha256=command.chunker_configuration_sha256,
        containment_policy="single-section-v1",
    )


class SqlAlchemySourceIngestionAuthority:
    """PostgreSQL authority adapter; every method owns one short atomic transaction."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    def _new_run(self, command: SourceIngestionInput, occurred_at: datetime) -> NewParseRun:
        return NewParseRun(
            scope=SourceScope(tenant_id=command.tenant_id, project_id=command.project_id),
            parse_run_id=command.parse_run_id,
            source_document_id=command.source_document_id,
            source_content_sha256=command.content_sha256,
            profile=_profile(command),
            started_at=occurred_at,
            request_id=command.request_id,
            correlation_id=command.correlation_id,
            expected_execution_configuration_sha256=(command.execution_configuration_sha256),
            causation_id=command.causation_id,
        )

    async def begin(self, command: SourceIngestionInput, occurred_at: datetime) -> None:
        async with self._sessions() as session, session.begin():
            service = SourceApplicationService(SqlAlchemySourceUnitOfWork(session))
            await service.begin_parse(self._new_run(command, occurred_at))

    async def _load_authority(
        self, session: AsyncSession, command: SourceIngestionInput
    ) -> tuple[SourceDocument, SourceParseRun]:
        source = await self._load_source(session, command)
        run = await self._load_run(session, command)
        if run is None:
            raise SourceConflictError("source ingestion authority does not match")
        if run.status not in {"running", "succeeded", "superseded"}:
            raise SourceConflictError("source parse run is not active")
        return source, run

    async def _load_source(
        self, session: AsyncSession, command: SourceIngestionInput
    ) -> SourceDocument:
        source = await session.scalar(
            select(SourceDocument).where(
                SourceDocument.tenant_id == command.tenant_id,
                SourceDocument.project_id == command.project_id,
                SourceDocument.id == command.source_document_id,
                SourceDocument.content_sha256 == command.content_sha256,
                SourceDocument.object_key == command.object_key,
                SourceDocument.byte_count == command.byte_size,
                SourceDocument.detected_media_type == command.media_type,
            )
        )
        if source is None:
            raise SourceConflictError("source ingestion authority does not match")
        return source

    async def _load_run(
        self, session: AsyncSession, command: SourceIngestionInput
    ) -> SourceParseRun | None:
        return cast(
            SourceParseRun | None,
            await session.scalar(
                select(SourceParseRun).where(
                    SourceParseRun.tenant_id == command.tenant_id,
                    SourceParseRun.project_id == command.project_id,
                    SourceParseRun.id == command.parse_run_id,
                    SourceParseRun.source_document_id == command.source_document_id,
                    SourceParseRun.source_content_sha256 == command.content_sha256,
                    SourceParseRun.parser_name == command.parser_name,
                    SourceParseRun.parser_version == command.parser_version,
                    SourceParseRun.parser_config_sha256 == command.parser_configuration_sha256,
                    SourceParseRun.chunker_version == command.chunker_version,
                    SourceParseRun.expected_chunker_name == command.chunker_name,
                    SourceParseRun.expected_chunker_config_sha256
                    == command.chunker_configuration_sha256,
                    SourceParseRun.expected_containment_policy == "single-section-v1",
                    SourceParseRun.identity_schema_version == 1,
                    SourceParseRun.expected_execution_config_sha256
                    == command.execution_configuration_sha256,
                )
            ),
        )

    async def verify(self, command: SourceIngestionInput) -> None:
        async with self._sessions() as session, session.begin():
            await self._load_authority(session, command)

    async def persist(
        self,
        command: SourceIngestionInput,
        product: ChunkedDocument,
        occurred_at: datetime,
    ) -> SourceIngestionOutput:
        async with self._sessions() as session, session.begin():
            await self._load_authority(session, command)
            service = SourceApplicationService(SqlAlchemySourceUnitOfWork(session))
            source = await service.persist_parse_product(
                PersistParseProduct(
                    scope=SourceScope(tenant_id=command.tenant_id, project_id=command.project_id),
                    source_document_id=command.source_document_id,
                    source_content_sha256=command.content_sha256,
                    parse_run_id=command.parse_run_id,
                    profile=_profile(command),
                    product=product,
                    occurred_at=occurred_at,
                    request_id=command.request_id,
                    correlation_id=command.correlation_id,
                    causation_id=command.causation_id,
                )
            )
            return SourceIngestionOutput(
                section_count=len(product.sections),
                chunk_count=len(product.chunks),
                product_version=source.aggregate_version,
            )

    async def record_failure(
        self,
        command: SourceIngestionInput,
        error_code: str,
        stage: str,
        occurred_at: datetime,
    ) -> None:
        async with self._sessions() as session, session.begin():
            source = await self._load_source(session, command)
            run = await self._load_run(session, command)
            if source.status == "chunks_ready" or (
                run is not None and run.status in {"succeeded", "superseded"}
            ):
                return
            if run is None and source.status not in {"accepted", "failed"}:
                raise SourceConflictError("another parse run owns the source")
            service = SourceApplicationService(SqlAlchemySourceUnitOfWork(session))
            failed_stage: FailedStage = "acceptance" if stage == "authority" else "parsing"
            await service.fail(
                scope=SourceScope(tenant_id=command.tenant_id, project_id=command.project_id),
                source=source,
                parse_run=run,
                profile=_profile(command) if run is not None else None,
                failure_code=error_code,
                failed_stage=failed_stage,
                occurred_at=occurred_at,
                event_id=uuid.uuid5(
                    uuid.NAMESPACE_URL,
                    f"source-ingestion-failed:{command.fingerprint()}:{error_code}",
                ),
                request_id=command.request_id,
                correlation_id=command.correlation_id,
                causation_id=command.causation_id,
            )


async def serve_worker_until_stopped(
    worker: AsyncWorkerContext, shutdown_requested: asyncio.Event
) -> None:
    async with worker:
        await shutdown_requested.wait()


class ProjectActivities:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        project_repository_factory: ProjectRepositoryFactory = SqlAlchemyProjectRepository,
        outbox_repository_factory: OutboxRepositoryFactory = SqlAlchemyOutboxRepository,
        service_factory: ServiceFactory = ProjectService,
    ) -> None:
        self._session_factory = session_factory
        self._project_repository_factory = project_repository_factory
        self._outbox_repository_factory = outbox_repository_factory
        self._service_factory = service_factory

    @activity.defn(name="create_project_activity")
    async def create_project_activity(
        self, command: InitializeProjectInput
    ) -> InitializeProjectOutput:
        async with self._session_factory() as session:
            projects = self._project_repository_factory(session)
            existing = await projects.get_by_request_id(command.tenant_id, command.request_id)
            if existing is not None:
                return InitializeProjectOutput(
                    project_id=existing.project.id,
                    branch_id=existing.default_branch.id,
                )

            outbox = self._outbox_repository_factory(session)
            service = self._service_factory(projects, outbox)
            try:
                created = await service.create(
                    command.tenant_id,
                    command.actor_id,
                    CreateProject(
                        request_id=command.request_id,
                        title=command.title,
                        language=command.language,
                    ),
                )
                await session.commit()
            except Exception:
                await session.rollback()
                raise
            return InitializeProjectOutput(
                project_id=created.project.id,
                branch_id=created.default_branch.id,
            )


def build_worker(
    client: Client,
    settings: Settings,
    project_activities: ProjectActivities,
    source_activities: SourceIngestionActivities,
    full_analysis_activities: FullAnalysisActivities,
) -> Worker:
    """Register every production workflow/activity explicitly at one composition root."""
    return Worker(
        client,
        task_queue=settings.temporal_task_queue,
        workflows=[
            InitializeProjectWorkflow,
            SourceIngestionWorkflow,
            FullAnalysisWorkflow,
        ],
        activities=[
            project_activities.create_project_activity,
            source_activities.begin_source_ingestion_activity,
            source_activities.process_source_ingestion_activity,
            source_activities.record_source_ingestion_failure_activity,
            full_analysis_activities.next_batch_index,
            full_analysis_activities.analyze_batch,
            full_analysis_activities.finalize,
        ],
        graceful_shutdown_timeout=GRACEFUL_SHUTDOWN_TIMEOUT,
    )


def _build_production_worker(client: Client, settings: Settings, engine: AsyncEngine) -> Worker:
    session_factory = create_session_factory(engine)
    project_activities = ProjectActivities(session_factory)
    assert settings.minio_endpoint_url is not None
    assert settings.minio_access_key is not None
    assert settings.minio_secret_key is not None
    assert settings.minio_bucket is not None
    assert settings.minio_receipt_signing_key is not None
    assert settings.minio_retention_verification_key is not None
    object_store = MinioObjectStore.from_settings(
        endpoint_url=settings.minio_endpoint_url,
        access_key=settings.minio_access_key.get_secret_value(),
        secret_key=settings.minio_secret_key.get_secret_value(),
        bucket=settings.minio_bucket,
        receipt_signing_key=settings.minio_receipt_signing_key.get_secret_value().encode(),
        retention_verification_key=(
            settings.minio_retention_verification_key.get_secret_value().encode()
        ),
        allow_insecure_local=settings.minio_allow_insecure_local,
    )
    parser_router = ParserRouter(
        [
            cast(DocumentParser, StdlibTxtParser()),
            cast(DocumentParser, StdlibDocxParser()),
            cast(DocumentParser, PdfTextParser.from_default()),
        ]
    )
    source_activities = SourceIngestionActivities(
        authority=SqlAlchemySourceIngestionAuthority(session_factory),
        object_reader=object_store,
        parser_router=parser_router,
    )
    full_analysis_service = FullAnalysisJobService(
        session_factory,
        DeepSeekAnalysisProvider(settings),
        gateway=_NullFullAnalysisGateway(),
    )
    full_analysis_activities = FullAnalysisActivities(full_analysis_service)
    return build_worker(
        client,
        settings,
        project_activities,
        source_activities,
        full_analysis_activities,
    )


async def run_worker(settings: Settings) -> None:
    required = {
        "MINIO_ENDPOINT_URL": settings.minio_endpoint_url,
        "MINIO_ACCESS_KEY": settings.minio_access_key,
        "MINIO_SECRET_KEY": settings.minio_secret_key,
        "MINIO_BUCKET": settings.minio_bucket,
        "MINIO_RECEIPT_SIGNING_KEY": settings.minio_receipt_signing_key,
        "MINIO_RETENTION_VERIFICATION_KEY": settings.minio_retention_verification_key,
    }

    def missing_value(value: object) -> bool:
        if value is None:
            return True
        reveal = getattr(value, "get_secret_value", None)
        if callable(reveal):
            value = reveal()
        return isinstance(value, str) and not value.strip()

    missing = [name for name, value in required.items() if missing_value(value)]
    if missing:
        raise RuntimeError(
            "source-ingestion worker configuration is incomplete: " + ", ".join(missing)
        )
    client = await Client.connect(
        settings.temporal_target,
        namespace=settings.temporal_namespace,
    )
    engine = create_engine(settings)
    try:
        worker = _build_production_worker(client, settings, engine)
    except BaseException:
        await engine.dispose()
        raise
    shutdown_requested = asyncio.Event()
    loop = asyncio.get_running_loop()
    previous_handlers: dict[
        signal.Signals, Callable[[int, FrameType | None], object] | int | None
    ] = {}

    def request_shutdown(_signum: int, _frame: FrameType | None) -> None:
        loop.call_soon_threadsafe(shutdown_requested.set)

    for signum in (signal.SIGINT, signal.SIGTERM):
        previous_handlers[signum] = signal.signal(signum, request_shutdown)

    try:
        await serve_worker_until_stopped(worker, shutdown_requested)
    finally:
        for signum, previous in previous_handlers.items():
            signal.signal(signum, previous)
        await engine.dispose()


def main() -> None:
    asyncio.run(run_worker(get_settings()))


if __name__ == "__main__":
    main()
