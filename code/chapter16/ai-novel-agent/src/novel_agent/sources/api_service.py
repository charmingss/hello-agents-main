from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, cast
from unicodedata import category

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novel_agent.chunking.novel import NovelChunker, NovelChunkerConfig
from novel_agent.config import Settings
from novel_agent.db.models.source import SourceDocument
from novel_agent.parsing.contracts import DocumentParser, ParserBudgets, ParseRequest
from novel_agent.parsing.docx import StdlibDocxParser
from novel_agent.parsing.pdf import PdfTextParser
from novel_agent.parsing.router import ParserRouter
from novel_agent.parsing.txt import StdlibTxtParser
from novel_agent.sources.contracts import (
    AuthorizationDeclaration,
    AuthorizationProvenance,
    CompleteSourceUpload,
    InitiateSourceUpload,
    SourceFileType,
    SourceMediaType,
    SourceScope,
)
from novel_agent.sources.errors import (
    AuthorizationProvenanceError,
    SourceConflictError,
    SourceHashMismatchError,
    SourceSizeLimitError,
    UploadIntentExpiredError,
    UploadQuotaExceededError,
    UploadValidationInProgressError,
)
from novel_agent.sources.repository import NewAcceptedSource
from novel_agent.sources.service import SourceApplicationService, SqlAlchemySourceUnitOfWork
from novel_agent.sources.upload_repository import SqlAlchemyUploadRepository
from novel_agent.storage.keys import ImmutableObjectKey, ObjectKeyFactory, StorageScope
from novel_agent.storage.ports import (
    ContentHashMismatch,
    ContentSizeMismatch,
    InvalidMediaSignature,
    ObjectNotFound,
    ObjectStorageError,
    ObjectStore,
    ObjectValidationRequest,
    ObjectValidationSpec,
)
from novel_agent.workflows.client import WorkflowServiceUnavailableError
from novel_agent.workflows.source_ingestion import (
    SourceIngestionInput,
    execution_configuration_sha256,
    source_ingestion_workflow_id,
)


class SourceNotFound(Exception):
    pass


UploadIntentExpired = UploadIntentExpiredError
UploadValidationInProgress = UploadValidationInProgressError


class InvalidSourceCursor(Exception):
    pass


class CorruptSource(Exception):
    pass


class StorageUnavailable(Exception):
    pass


class SourceIngestionWorkflowGateway(Protocol):
    async def start(
        self,
        *,
        workflow_id: str,
        command: SourceIngestionInput,
    ) -> str | None: ...


@dataclass(frozen=True)
class InitiatedUpload:
    source_id: uuid.UUID
    upload_id: uuid.UUID
    upload_url: str
    required_headers: Mapping[str, str]
    expires_at: datetime


@dataclass(frozen=True)
class CompletedSource:
    source_id: uuid.UUID
    filename: str
    media_type: str
    byte_size: int
    content_sha256: str
    status: str
    accepted_at: datetime


@dataclass(frozen=True)
class SourceSummary:
    source_id: uuid.UUID
    filename: str
    media_type: str
    byte_size: int
    content_sha256: str
    status: str
    failure_code: str | None
    created_at: datetime
    accepted_at: datetime | None


@dataclass(frozen=True)
class SourcePage:
    items: list[SourceSummary]
    next_cursor: str | None


def _encode_cursor(created_at: datetime, source_id: uuid.UUID) -> str:
    payload = json.dumps(
        {"created_at": created_at.isoformat(), "source_id": str(source_id)},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).decode().rstrip("=")


def _decode_cursor(value: str | None) -> tuple[datetime, uuid.UUID] | None:
    if value is None:
        return None
    try:
        raw = base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {"created_at", "source_id"}:
            raise ValueError
        created_at = datetime.fromisoformat(payload["created_at"])
        if created_at.tzinfo is None or created_at.utcoffset() is None:
            raise ValueError
        return created_at, uuid.UUID(payload["source_id"])
    except (binascii.Error, json.JSONDecodeError, TypeError, ValueError):
        raise InvalidSourceCursor from None


def _source_summary(source: SourceDocument) -> SourceSummary:
    return SourceSummary(
        source_id=source.id,
        filename=source.original_display_name,
        media_type=source.detected_media_type,
        byte_size=source.byte_count,
        content_sha256=source.content_sha256,
        status=source.status,
        failure_code=source.failure_code,
        created_at=source.created_at,
        accepted_at=source.accepted_at,
    )


def _provenance_hash(provenance: AuthorizationProvenance) -> str:
    encoded = json.dumps(
        provenance.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


def _completion_id(upload_id: uuid.UUID, command: CompleteSourceUpload) -> str:
    material = f"{upload_id}:{command.byte_size}:{command.content_sha256}".encode()
    return "complete-" + hashlib.sha256(material).hexdigest()


def _validate_actor_id(actor_id: str) -> None:
    if not 1 <= len(actor_id) <= 255 or any(
        category(character) in {"Cc", "Cf", "Zl", "Zp"} for character in actor_id
    ):
        raise AuthorizationProvenanceError("authenticated subject is not audit-safe")


class SourceApiService:
    """Coordinates short DB claims around blocking storage work."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        object_store: ObjectStore | None,
        workflow_gateway: SourceIngestionWorkflowGateway | None,
        settings: Settings,
    ) -> None:
        self._sessions = session_factory
        self._store = object_store
        self._workflows = workflow_gateway
        self._settings = settings
        self._keys = ObjectKeyFactory()

    async def initiate(
        self,
        *,
        tenant_id: uuid.UUID,
        project_id: uuid.UUID,
        actor_id: str,
        command: InitiateSourceUpload,
    ) -> InitiatedUpload:
        _validate_actor_id(actor_id)
        if self._store is None:
            raise StorageUnavailable
        if command.byte_size > self._settings.max_upload_bytes:
            raise SourceSizeLimitError
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=self._settings.upload_intent_ttl_seconds)
        identity = f"{tenant_id}:{project_id}:{command.request_id}"
        source_id = uuid.uuid5(uuid.NAMESPACE_URL, f"source:{identity}")
        upload_id = uuid.uuid5(uuid.NAMESPACE_URL, f"upload:{identity}")
        async with self._sessions() as session, session.begin():
            repository = SqlAlchemyUploadRepository(session)
            if not await repository.project_exists(tenant_id, project_id):
                raise SourceNotFound("project")
            row = await repository.create_or_get(
                tenant_id=tenant_id,
                project_id=project_id,
                source_id=source_id,
                upload_id=upload_id,
                command=command,
                actor_id=actor_id,
                expires_at=expires_at,
                max_pending=self._settings.max_pending_upload_sessions_per_project,
            )
            source_id, upload_id, expires_at = (
                row.source_document_id,
                row.upload_id,
                row.expires_at,
            )
            expired = row.status == "expired"
        if expired:
            raise UploadIntentExpired
        scope = StorageScope(tenant_id=tenant_id, project_id=project_id)
        try:
            intent = await asyncio.to_thread(
                self._store.create_upload_intent,
                scope=scope,
                source_id=source_id,
                upload_id=upload_id,
                expires_in=max(expires_at - datetime.now(UTC), timedelta(seconds=1)),
            )
        except ObjectStorageError as exc:
            raise StorageUnavailable from exc
        return InitiatedUpload(
            source_id=source_id,
            upload_id=upload_id,
            upload_url=intent.upload_url,
            required_headers=intent.required_headers,
            expires_at=intent.expires_at,
        )

    async def complete(
        self,
        *,
        tenant_id: uuid.UUID,
        project_id: uuid.UUID,
        actor_id: str,
        command: CompleteSourceUpload,
    ) -> CompletedSource:
        _validate_actor_id(actor_id)
        now = datetime.now(UTC)
        completion_id = _completion_id(command.upload_id, command)
        async with self._sessions() as session, session.begin():
            repository = SqlAlchemyUploadRepository(session)
            if not await repository.project_exists(tenant_id, project_id):
                raise SourceNotFound("project")
            claim = await repository.claim(
                tenant_id=tenant_id,
                project_id=project_id,
                upload_id=command.upload_id,
                completion_request_id=completion_id,
                actor_id=actor_id,
                expected_byte_count=command.byte_size,
                expected_sha256=command.content_sha256,
                now=now,
                lease_expires_at=now
                + timedelta(seconds=self._settings.upload_validation_lease_seconds),
            )
            if claim is None:
                raise SourceNotFound("upload")
            if claim.already_consumed:
                source = await repository.get_source(
                    tenant_id, project_id, claim.session.source_document_id
                )
                if source is None:
                    raise SourceConflictError("consumed upload has no source")
                return CompletedSource(
                    source_id=source.id,
                    filename=source.original_display_name,
                    media_type=source.detected_media_type,
                    byte_size=source.byte_count,
                    content_sha256=source.content_sha256,
                    status=source.status,
                    accepted_at=source.accepted_at or source.created_at,
                )
            if claim.in_progress:
                raise UploadValidationInProgress
            expired = claim.session.status == "expired" or claim.attempt_token is None
            if expired:
                source_id = claim.session.source_document_id
            else:
                source_id = claim.session.source_document_id
                row = claim.session
                filename = row.original_display_name
                file_type = row.file_type
                media_type = row.declared_media_type
                # JSON storage represents enums/sets as strings/lists; revalidate the
                # persisted declaration while allowing only that JSON coercion boundary.
                declaration = AuthorizationDeclaration.model_validate(
                    row.authorization_declaration, strict=False
                )
                declared_by_subject = row.declared_by_subject
                attempt_token = claim.attempt_token

        if expired:
            raise UploadIntentExpired
        assert attempt_token is not None
        store = self._store
        if store is None:
            await self._release_claim(tenant_id, project_id, command.upload_id, attempt_token)
            raise StorageUnavailable

        scope = StorageScope(tenant_id=tenant_id, project_id=project_id)
        staging = self._keys.staging(scope=scope, source_id=source_id, upload_id=command.upload_id)
        request = ObjectValidationRequest(
            key=staging,
            file_type=SourceFileType(file_type),
            declared_media_type=cast(SourceMediaType, media_type),
            expected_byte_size=command.byte_size,
            expected_sha256=command.content_sha256,
            max_bytes=self._settings.max_upload_bytes,
            max_archive_entries=self._settings.max_archive_entries,
            max_extracted_bytes=self._settings.max_extracted_bytes,
            max_archive_entry_bytes=self._settings.max_archive_entry_bytes,
            max_compression_ratio=self._settings.max_compression_ratio,
        )
        destination = self._keys.immutable(
            scope=scope, source_id=source_id, sha256=command.content_sha256
        )
        spec = ObjectValidationSpec.model_validate(request.model_dump(exclude={"key"}), strict=True)
        try:
            receipt = await asyncio.to_thread(store.validate, scope, request)
            promoted = await asyncio.to_thread(store.promote, scope, receipt, destination)
        except ObjectNotFound:
            try:
                promoted = await asyncio.to_thread(store.recover_promoted, scope, destination, spec)
            except ContentHashMismatch as exc:
                await self._release_claim(tenant_id, project_id, command.upload_id, attempt_token)
                raise SourceHashMismatchError from exc
            except (ContentSizeMismatch, InvalidMediaSignature) as exc:
                await self._release_claim(tenant_id, project_id, command.upload_id, attempt_token)
                raise CorruptSource("invalid_promoted_content") from exc
            except ObjectStorageError as exc:
                await self._release_claim(tenant_id, project_id, command.upload_id, attempt_token)
                raise StorageUnavailable from exc
        except ContentHashMismatch as exc:
            await self._release_claim(tenant_id, project_id, command.upload_id, attempt_token)
            raise SourceHashMismatchError from exc
        except ContentSizeMismatch as exc:
            await self._release_claim(tenant_id, project_id, command.upload_id, attempt_token)
            raise CorruptSource("size_mismatch") from exc
        except InvalidMediaSignature as exc:
            await self._release_claim(tenant_id, project_id, command.upload_id, attempt_token)
            raise CorruptSource("invalid_media") from exc
        except ObjectStorageError as exc:
            await self._release_claim(tenant_id, project_id, command.upload_id, attempt_token)
            raise StorageUnavailable from exc

        accepted_at = datetime.now(UTC)
        provenance = AuthorizationProvenance(
            declaration=declaration,
            attested_by_subject=declared_by_subject,
            attested_at=accepted_at,
        )
        try:
            async with self._sessions() as session, session.begin():
                repository = SqlAlchemyUploadRepository(session)
                finalized = await repository.finalize(
                    tenant_id=tenant_id,
                    project_id=project_id,
                    upload_id=command.upload_id,
                    attempt_token=attempt_token,
                    consumed_at=accepted_at,
                )
                if finalized is None:
                    raise SourceConflictError("upload completion lease was superseded")
                application = SourceApplicationService(SqlAlchemySourceUnitOfWork(session))
                source = await application.accept_source(
                    NewAcceptedSource(
                        scope=SourceScope(tenant_id=tenant_id, project_id=project_id),
                        source_document_id=source_id,
                        upload_request_id=finalized.request_id,
                        authorization_provenance=provenance.model_dump(mode="json"),
                        authorization_sha256=_provenance_hash(provenance),
                        original_display_name=filename,
                        declared_media_type=cast(SourceMediaType, media_type),
                        detected_media_type=promoted.media_type,
                        byte_count=promoted.byte_size,
                        content_sha256=promoted.sha256,
                        object_key=promoted.key.value,
                        accepted_at=accepted_at,
                        event_id=uuid.uuid5(uuid.NAMESPACE_URL, f"accepted:{command.upload_id}"),
                        correlation_id=uuid.uuid5(
                            uuid.NAMESPACE_URL, f"upload:{command.upload_id}"
                        ),
                    )
                )
        except Exception:
            await self._release_claim(tenant_id, project_id, command.upload_id, attempt_token)
            raise
        return CompletedSource(
            source_id=source.id,
            filename=source.original_display_name,
            media_type=source.detected_media_type,
            byte_size=source.byte_count,
            content_sha256=source.content_sha256,
            status=source.status,
            accepted_at=source.accepted_at or accepted_at,
        )

    async def _release_claim(
        self,
        tenant_id: uuid.UUID,
        project_id: uuid.UUID,
        upload_id: uuid.UUID,
        attempt_token: uuid.UUID,
    ) -> None:
        try:
            async with self._sessions() as session, session.begin():
                await SqlAlchemyUploadRepository(session).release(
                    tenant_id=tenant_id,
                    project_id=project_id,
                    upload_id=upload_id,
                    attempt_token=attempt_token,
                )
        except Exception:
            # Keep the original stable storage error. The finite lease remains recoverable.
            return

    async def list_sources(
        self,
        *,
        tenant_id: uuid.UUID,
        project_id: uuid.UUID,
        limit: int = 50,
        cursor: str | None = None,
    ) -> SourcePage:
        decoded = _decode_cursor(cursor)
        async with self._sessions() as session, session.begin():
            repository = SqlAlchemyUploadRepository(session)
            if not await repository.project_exists(tenant_id, project_id):
                raise SourceNotFound("project")
            rows = await repository.list_sources_page(
                tenant_id, project_id, limit=limit, cursor=decoded
            )
            has_more = len(rows) > limit
            page_rows = rows[:limit]
            next_cursor = None
            if has_more and page_rows:
                last = page_rows[-1]
                next_cursor = _encode_cursor(last.created_at, last.id)
            return SourcePage(
                items=[_source_summary(source) for source in page_rows],
                next_cursor=next_cursor,
            )

    async def get_source(
        self, *, tenant_id: uuid.UUID, project_id: uuid.UUID, source_id: uuid.UUID
    ) -> SourceSummary:
        async with self._sessions() as session, session.begin():
            repository = SqlAlchemyUploadRepository(session)
            if not await repository.project_exists(tenant_id, project_id):
                raise SourceNotFound("project")
            source = await repository.get_source(tenant_id, project_id, source_id)
            if source is None:
                raise SourceNotFound("source")
            return _source_summary(source)

    async def start_ingestion(
        self, *, tenant_id: uuid.UUID, project_id: uuid.UUID, source_id: uuid.UUID
    ) -> str:
        async with self._sessions() as session, session.begin():
            repository = SqlAlchemyUploadRepository(session)
            if not await repository.project_exists(tenant_id, project_id):
                raise SourceNotFound("project")
            source = await repository.get_source(tenant_id, project_id, source_id)
            if source is None:
                raise SourceNotFound("source")
            if source.status not in {"accepted", "parsing", "chunks_ready", "failed"}:
                raise SourceConflictError("source is not ready to start ingestion")
        if self._workflows is None:
            raise WorkflowServiceUnavailableError
        store = self._store
        if store is None:
            raise StorageUnavailable
        scope = StorageScope(tenant_id=tenant_id, project_id=project_id)
        key = ImmutableObjectKey(value=source.object_key)
        try:
            metadata = await asyncio.to_thread(store.stat, scope, key)
        except ObjectStorageError as exc:
            raise StorageUnavailable from exc
        if (
            metadata.key.value != source.object_key
            or metadata.byte_size != source.byte_count
            or metadata.stored_sha256 not in {None, source.content_sha256}
        ):
            raise CorruptSource("immutable_object_conflict")
        media_to_type = {
            "text/plain": SourceFileType.TXT,
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (
                SourceFileType.DOCX
            ),
            "application/pdf": SourceFileType.PDF,
        }
        file_type = media_to_type.get(source.detected_media_type)
        if file_type is None:
            raise CorruptSource("unsupported_media")
        budgets = ParserBudgets(
            max_input_bytes=self._settings.max_upload_bytes,
            max_pages=self._settings.max_pdf_pages,
            max_xml_entries=self._settings.max_archive_entries,
            max_xml_entry_bytes=self._settings.max_archive_entry_bytes,
            max_xml_uncompressed_bytes=self._settings.max_extracted_bytes,
            max_compression_ratio=self._settings.max_compression_ratio,
            max_extracted_bytes=self._settings.max_extracted_bytes,
        )
        parse_request = ParseRequest(
            source_id=source.id,
            file_type=file_type,
            media_type=cast(SourceMediaType, source.detected_media_type),
            byte_size=source.byte_count,
            content_sha256=source.content_sha256,
            budgets=budgets,
        )
        parsers = cast(
            list[DocumentParser],
            [StdlibTxtParser(), StdlibDocxParser(), PdfTextParser.from_default()],
        )
        parser = ParserRouter(parsers).select(parse_request)
        parser_identity = parser.identity
        chunker_config = NovelChunkerConfig()
        chunker = NovelChunker(version=self._settings.chunker_version, config=chunker_config)
        execution_config_sha256 = execution_configuration_sha256(budgets)
        identity_material = (
            f"{tenant_id}:{project_id}:{source_id}:{source.content_sha256}:"
            f"{parser_identity.name}:{parser_identity.version}:"
            f"{parser_identity.configuration_sha256}:{chunker.identity.name}:"
            f"{chunker.identity.version}:{chunker.identity.configuration_sha256}:"
            f"{execution_config_sha256}"
        )
        parse_run_id = uuid.uuid5(uuid.NAMESPACE_URL, f"source-parse:{identity_material}")
        command = SourceIngestionInput(
            tenant_id=tenant_id,
            project_id=project_id,
            source_document_id=source.id,
            parse_run_id=parse_run_id,
            request_id=f"ingest-{source.id.hex}",
            correlation_id=uuid.uuid5(uuid.NAMESPACE_URL, f"source:{source.id}"),
            object_key=source.object_key,
            object_etag=metadata.etag,
            object_version_id=metadata.version_id,
            content_sha256=source.content_sha256,
            byte_size=source.byte_count,
            file_type=file_type,
            media_type=cast(SourceMediaType, source.detected_media_type),
            parser_name=parser_identity.name,
            parser_version=parser_identity.version,
            parser_configuration_sha256=parser_identity.configuration_sha256,
            parser_budgets=budgets,
            execution_configuration_sha256=execution_config_sha256,
            chunker_name=chunker.identity.name,
            chunker_version=chunker.identity.version,
            chunker_configuration_sha256=chunker.identity.configuration_sha256,
            chunker_config=chunker_config,
        )
        workflow_id = source_ingestion_workflow_id(command)
        await self._workflows.start(
            workflow_id=workflow_id,
            command=command,
        )
        return workflow_id


__all__ = [
    "CompletedSource",
    "CorruptSource",
    "InitiatedUpload",
    "InvalidSourceCursor",
    "SourceApiService",
    "SourceIngestionWorkflowGateway",
    "SourceNotFound",
    "SourceSummary",
    "SourcePage",
    "StorageUnavailable",
    "UploadIntentExpired",
    "UploadValidationInProgress",
    "UploadQuotaExceededError",
]
