from __future__ import annotations

import asyncio
import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import timedelta
from typing import Literal, cast

from temporalio import workflow
from temporalio.common import RetryPolicy

from novel_agent.chunking.novel import NovelChunker, NovelChunkerConfig
from novel_agent.parsing.contracts import (
    EncryptedDocument,
    ExtractedContentTooLarge,
    InvalidDocumentContainer,
    InvalidTextContent,
    MalformedDocument,
    MissingParserDependency,
    OcrNotSupported,
    ParserBudgets,
    ParserError,
    ParserSelectionError,
    SourceContentTooLarge,
)
from novel_agent.sources.contracts import SourceFileType, SourceMediaType
from novel_agent.storage.keys import ImmutableObjectKey, StorageScope

IngestionState = Literal["pending", "running", "completed", "failed", "canceled"]
INGEST_ACTIVITY_MAXIMUM_ATTEMPTS = 4
BEGIN_ACTIVITY_TIMEOUT = timedelta(seconds=30)
PROCESS_ACTIVITY_TIMEOUT = timedelta(minutes=10)
AUDIT_ACTIVITY_TIMEOUT = timedelta(seconds=30)


@dataclass(frozen=True)
class SourceIngestionInput:
    tenant_id: uuid.UUID
    project_id: uuid.UUID
    source_document_id: uuid.UUID
    parse_run_id: uuid.UUID
    request_id: str
    correlation_id: uuid.UUID
    object_key: str
    object_etag: str
    object_version_id: str | None
    content_sha256: str
    byte_size: int
    file_type: SourceFileType
    media_type: SourceMediaType
    parser_name: str
    parser_version: str
    parser_configuration_sha256: str
    parser_budgets: ParserBudgets
    execution_configuration_sha256: str
    chunker_name: str
    chunker_version: str
    chunker_configuration_sha256: str
    chunker_config: NovelChunkerConfig
    causation_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if not self.request_id or len(self.request_id) > 100:
            raise ValueError("request_id must be bounded")
        if self.byte_size <= 0 or self.byte_size > self.parser_budgets.max_input_bytes:
            raise ValueError("byte_size exceeds parser input budget")
        if len(self.content_sha256) != 64 or any(
            c not in "0123456789abcdef" for c in self.content_sha256
        ):
            raise ValueError("content_sha256 must be lowercase hexadecimal")
        for value, label in (
            (self.parser_configuration_sha256, "parser configuration"),
            (self.chunker_configuration_sha256, "chunker configuration"),
            (self.execution_configuration_sha256, "execution configuration"),
        ):
            if len(value) != 64 or any(c not in "0123456789abcdef" for c in value):
                raise ValueError(f"{label} sha256 must be lowercase hexadecimal")
        expected_execution = execution_configuration_sha256(self.parser_budgets)
        if self.execution_configuration_sha256 != expected_execution:
            raise ValueError("execution configuration hash does not match parser budgets")
        chunker = NovelChunker(version=self.chunker_version, config=self.chunker_config)
        if (
            chunker.identity.name != self.chunker_name
            or chunker.identity.configuration_sha256 != self.chunker_configuration_sha256
        ):
            raise ValueError("chunker identity does not match canonical configuration")
        key = ImmutableObjectKey(value=self.object_key)
        scope = StorageScope(tenant_id=self.tenant_id, project_id=self.project_id)
        if not key.belongs_to(scope) or key.parts[-1] != self.content_sha256:
            raise ValueError("immutable object key does not match source scope and content")
        expected_media: dict[SourceFileType, str] = {
            SourceFileType.TXT: "text/plain",
            SourceFileType.DOCX: (
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            ),
            SourceFileType.PDF: "application/pdf",
        }
        if self.media_type != expected_media[self.file_type]:
            raise ValueError("media type does not match source file type")

    def fingerprint(self) -> str:
        values = {
            "tenant_id": str(self.tenant_id),
            "project_id": str(self.project_id),
            "source_document_id": str(self.source_document_id),
            "parse_run_id": str(self.parse_run_id),
            "request_id": self.request_id,
            "correlation_id": str(self.correlation_id),
            "causation_id": (str(self.causation_id) if self.causation_id is not None else None),
            "object_key": self.object_key,
            "object_etag": self.object_etag,
            "object_version_id": self.object_version_id,
            "content_sha256": self.content_sha256,
            "byte_size": self.byte_size,
            "file_type": self.file_type.value,
            "media_type": self.media_type,
            "parser": [self.parser_name, self.parser_version, self.parser_configuration_sha256],
            "parser_budgets": self.parser_budgets.model_dump(mode="json"),
            "execution_configuration_sha256": self.execution_configuration_sha256,
            "chunker": [
                self.chunker_name,
                self.chunker_version,
                self.chunker_configuration_sha256,
            ],
            "chunker_config": self.chunker_config.model_dump(mode="json"),
        }
        encoded = json.dumps(values, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()


def execution_configuration_sha256(budgets: ParserBudgets) -> str:
    encoded = json.dumps(
        budgets.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class SourceIngestionOutput:
    section_count: int
    chunk_count: int
    product_version: int


@dataclass(frozen=True)
class SourceIngestionFailure:
    command: SourceIngestionInput
    error_code: str
    stage: str


@dataclass(frozen=True)
class SourceIngestionStatus:
    state: IngestionState = "pending"
    stage: str = "pending"
    progress: int = 0
    error_code: str | None = None

    def __post_init__(self) -> None:
        if not 0 <= self.progress <= 100:
            raise ValueError("progress must be between zero and one hundred")
        if not self.stage or len(self.stage) > 64:
            raise ValueError("stage must be bounded")
        if self.error_code is not None and (
            len(self.error_code) > 64
            or not self.error_code.replace("_", "a").isalnum()
            or not self.error_code[0].isalpha()
        ):
            raise ValueError("error_code must be a stable token")


_PERMANENT_ERRORS: tuple[tuple[type[ParserError], str], ...] = (
    (InvalidTextContent, "invalid_text"),
    (InvalidDocumentContainer, "invalid_container"),
    (MalformedDocument, "malformed_document"),
    (EncryptedDocument, "encrypted_document"),
    (OcrNotSupported, "ocr_not_supported"),
    (SourceContentTooLarge, "source_too_large"),
    (ExtractedContentTooLarge, "extracted_content_too_large"),
    (ParserSelectionError, "parser_selection_failed"),
    (MissingParserDependency, "parser_dependency_missing"),
)
_PUBLIC_ERROR_CODES = frozenset(
    {
        "authority_conflict",
        "chunker_identity_conflict",
        "database_unavailable",
        "encrypted_document",
        "extracted_content_too_large",
        "immutable_object_conflict",
        "ingestion_canceled",
        "ingestion_failed",
        "ingestion_transient",
        "invalid_container",
        "invalid_text",
        "malformed_document",
        "ocr_not_supported",
        "parser_dependency_missing",
        "parser_identity_conflict",
        "parser_selection_failed",
        "source_too_large",
        "storage_unavailable",
    }
)


def permanent_error_code(error: BaseException) -> str | None:
    for error_type, code in _PERMANENT_ERRORS:
        if isinstance(error, error_type):
            return code
    return None


def source_ingestion_workflow_id(command: SourceIngestionInput) -> str:
    return (
        f"source-ingestion:{command.tenant_id}:{command.project_id}:"
        f"{command.source_document_id}:{command.parser_name}:{command.parser_version}:"
        f"{command.chunker_name}:{command.chunker_version}:{command.fingerprint()[:24]}"
    )


@workflow.defn
class SourceIngestionWorkflow:
    def __init__(self) -> None:
        self._status = SourceIngestionStatus()
        self._fingerprint: str | None = None

    @workflow.query
    def status(self) -> SourceIngestionStatus:
        return self._status

    @workflow.query
    def fingerprint(self) -> str | None:
        return self._fingerprint

    @workflow.run
    async def run(self, command: SourceIngestionInput) -> SourceIngestionOutput:
        self._fingerprint = command.fingerprint()
        try:
            self._status = SourceIngestionStatus("running", "authority", 5)
            await workflow.execute_activity(
                "begin_source_ingestion_activity",
                command,
                start_to_close_timeout=BEGIN_ACTIVITY_TIMEOUT,
                retry_policy=RetryPolicy(maximum_attempts=INGEST_ACTIVITY_MAXIMUM_ATTEMPTS),
                cancellation_type=(workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED),
            )
            self._status = SourceIngestionStatus("running", "parse_chunk_persist", 20)
            result = cast(
                SourceIngestionOutput,
                await workflow.execute_activity(
                    "process_source_ingestion_activity",
                    command,
                    start_to_close_timeout=PROCESS_ACTIVITY_TIMEOUT,
                    retry_policy=RetryPolicy(
                        initial_interval=timedelta(seconds=1),
                        backoff_coefficient=2.0,
                        maximum_interval=timedelta(seconds=30),
                        maximum_attempts=INGEST_ACTIVITY_MAXIMUM_ATTEMPTS,
                    ),
                    cancellation_type=(
                        workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED
                    ),
                    result_type=SourceIngestionOutput,
                ),
            )
        except asyncio.CancelledError:
            await self._audit(command, "ingestion_canceled")
            self._status = SourceIngestionStatus(
                "canceled", self._status.stage, self._status.progress, "ingestion_canceled"
            )
            raise
        except BaseException as exc:
            candidate = _application_error_type(exc)
            code = candidate if candidate in _PUBLIC_ERROR_CODES else "ingestion_failed"
            await self._audit(command, code)
            self._status = SourceIngestionStatus(
                "failed", self._status.stage, self._status.progress, code
            )
            raise
        self._status = SourceIngestionStatus("completed", "completed", 100)
        return result

    async def _audit(self, command: SourceIngestionInput, error_code: str) -> None:
        failure = SourceIngestionFailure(command, error_code, self._status.stage)
        try:
            await asyncio.shield(
                workflow.execute_activity(
                    "record_source_ingestion_failure_activity",
                    failure,
                    start_to_close_timeout=AUDIT_ACTIVITY_TIMEOUT,
                    retry_policy=RetryPolicy(maximum_attempts=INGEST_ACTIVITY_MAXIMUM_ATTEMPTS),
                )
            )
        except BaseException:
            # Preserve the original terminal result; the activity has finite retries and
            # authoritative state remains recoverable by reconciliation.
            return


def _application_error_type(error: BaseException) -> str | None:
    current: BaseException | None = error
    for _ in range(8):
        if current is None:
            return None
        value = getattr(current, "type", None)
        if isinstance(value, str) and value:
            return value
        current = current.__cause__
    return None


__all__ = [
    "AUDIT_ACTIVITY_TIMEOUT",
    "BEGIN_ACTIVITY_TIMEOUT",
    "INGEST_ACTIVITY_MAXIMUM_ATTEMPTS",
    "PROCESS_ACTIVITY_TIMEOUT",
    "SourceIngestionInput",
    "SourceIngestionFailure",
    "SourceIngestionOutput",
    "SourceIngestionStatus",
    "SourceIngestionWorkflow",
    "permanent_error_code",
    "execution_configuration_sha256",
    "source_ingestion_workflow_id",
]
