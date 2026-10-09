from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import Select, case, exists, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import ReturningInsert

from novel_agent.chunking.novel import (
    ChunkedDocument,
    canonical_product_sha256,
    validate_chunked_document,
)
from novel_agent.db.models.source import (
    ProjectionCheckpoint,
    SourceChunk,
    SourceDocument,
    SourceParseRun,
    SourceSection,
)
from novel_agent.sources.contracts import ParserProfile, SourceMediaType, SourceScope
from novel_agent.sources.errors import SourceConflictError, SourceStateTransitionError

_SECTION_INSERT_BATCH = 500
_CHUNK_INSERT_BATCH = 1_000


@dataclass(frozen=True)
class CreateResult[T]:
    value: T
    created: bool


@dataclass(frozen=True)
class NewAcceptedSource:
    scope: SourceScope
    source_document_id: uuid.UUID
    upload_request_id: str
    authorization_provenance: dict[str, Any]
    authorization_sha256: str
    original_display_name: str
    declared_media_type: SourceMediaType
    detected_media_type: SourceMediaType
    byte_count: int
    content_sha256: str
    object_key: str
    accepted_at: datetime
    event_id: uuid.UUID
    correlation_id: uuid.UUID
    causation_id: uuid.UUID | None = None


@dataclass(frozen=True)
class NewParseRun:
    scope: SourceScope
    parse_run_id: uuid.UUID
    source_document_id: uuid.UUID
    source_content_sha256: str
    profile: ParserProfile
    started_at: datetime
    request_id: str
    correlation_id: uuid.UUID
    expected_execution_configuration_sha256: str
    causation_id: uuid.UUID | None = None


@dataclass(frozen=True)
class PersistParseProduct:
    scope: SourceScope
    source_document_id: uuid.UUID
    source_content_sha256: str
    parse_run_id: uuid.UUID
    profile: ParserProfile
    product: ChunkedDocument
    occurred_at: datetime
    request_id: str
    correlation_id: uuid.UUID
    causation_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if (
            self.product.source_document_id != self.source_document_id
            or self.product.source_content_sha256 != self.source_content_sha256
        ):
            raise ValueError("product source lineage does not match persistence command")
        parser = self.product
        if (
            parser.parser_name != self.profile.parser_name
            or parser.parser_version != self.profile.parser_version
            or parser.parser_config_sha256 != self.profile.configuration_sha256
        ):
            raise ValueError("product parser identity does not match parse profile")
        if parser.identity.version != self.profile.chunker_version:
            raise ValueError("product chunker identity does not match parse profile")
        if (
            parser.identity.name != self.profile.chunker_name
            or parser.identity.configuration_sha256 != self.profile.chunker_configuration_sha256
            or parser.identity.containment_policy != self.profile.containment_policy
        ):
            raise ValueError("product chunker authority does not match parse profile")
        validate_chunked_document(parser)


@dataclass(frozen=True)
class PersistProductResult:
    source: SourceDocument
    parse_run: SourceParseRun
    product_version: int
    created: bool


def _source_location(source_ref: Any) -> dict[str, int | None]:
    page = source_ref.index + 1 if source_ref.kind == "page" else None
    paragraph = source_ref.index if source_ref.kind == "paragraph" else None
    return {
        "page_start": page,
        "page_end": page,
        "paragraph_start": paragraph,
        "paragraph_end": paragraph,
        "source_ref_kind": source_ref.kind,
        "source_ref_index": source_ref.index,
        "source_ref_subindex": source_ref.subindex,
    }


def section_row_values(value: PersistParseProduct) -> list[dict[str, Any]]:
    return [
        {
            "id": section.id,
            "tenant_id": value.scope.tenant_id,
            "project_id": value.scope.project_id,
            "parse_run_id": value.parse_run_id,
            "section_index": section.index,
            "heading": section.heading,
            "text": section.text,
            "char_start": section.char_start,
            "char_end": section.char_end,
            **_source_location(section.source_ref),
        }
        for section in value.product.sections
    ]


def chunk_row_values(value: PersistParseProduct) -> list[dict[str, Any]]:
    return [
        {
            "id": chunk.id,
            "tenant_id": value.scope.tenant_id,
            "project_id": value.scope.project_id,
            "parse_run_id": value.parse_run_id,
            "section_id": chunk.section_id,
            "chunk_index": chunk.index,
            "text": chunk.text,
            "token_count": None,
            "char_start": chunk.char_start,
            "char_end": chunk.char_end,
            **_source_location(chunk.source_ref),
        }
        for chunk in value.product.chunks
    ]


def authoritative_source_statement(
    value: PersistParseProduct,
) -> Select[tuple[SourceDocument]]:
    return (
        select(SourceDocument)
        .where(
            SourceDocument.tenant_id == value.scope.tenant_id,
            SourceDocument.project_id == value.scope.project_id,
            SourceDocument.id == value.source_document_id,
            SourceDocument.content_sha256 == value.source_content_sha256,
        )
        .with_for_update()
    )


def authoritative_product_statement(
    value: PersistParseProduct,
) -> Select[tuple[SourceParseRun]]:
    return (
        select(SourceParseRun)
        .where(
            SourceParseRun.tenant_id == value.scope.tenant_id,
            SourceParseRun.project_id == value.scope.project_id,
            SourceParseRun.id == value.parse_run_id,
            SourceParseRun.source_document_id == value.source_document_id,
            SourceParseRun.source_content_sha256 == value.source_content_sha256,
            SourceParseRun.parser_name == value.profile.parser_name,
            SourceParseRun.parser_version == value.profile.parser_version,
            SourceParseRun.parser_config_sha256 == value.profile.configuration_sha256,
            SourceParseRun.chunker_version == value.profile.chunker_version,
            SourceParseRun.expected_chunker_name == value.profile.chunker_name,
            SourceParseRun.expected_chunker_config_sha256
            == value.profile.chunker_configuration_sha256,
            SourceParseRun.expected_containment_policy == value.profile.containment_policy,
        )
        .with_for_update()
    )


def bounded_batches[T](values: list[T], size: int) -> list[list[T]]:
    if size <= 0:
        raise ValueError("batch size must be positive")
    return [values[index : index + size] for index in range(0, len(values), size)]


_SOURCE_TRANSITIONS: dict[str, frozenset[str]] = {
    "upload_pending": frozenset({"uploaded", "failed", "quarantined"}),
    "uploaded": frozenset({"accepted", "failed", "quarantined"}),
    "accepted": frozenset({"parsing", "failed", "quarantined"}),
    "parsing": frozenset({"parsed", "failed"}),
    "parsed": frozenset({"chunks_ready", "failed"}),
    "chunks_ready": frozenset(),
    "failed": frozenset(),
    "quarantined": frozenset(),
}
_PARSE_TRANSITIONS: dict[str, frozenset[str]] = {
    "pending": frozenset({"running", "failed"}),
    "running": frozenset({"succeeded", "failed"}),
    "succeeded": frozenset({"superseded"}),
    "failed": frozenset(),
    "superseded": frozenset(),
}


def _ensure_transition(current: str, target: str, transitions: dict[str, frozenset[str]]) -> None:
    if current == target:
        return
    if current not in transitions or target not in transitions[current]:
        raise SourceStateTransitionError(current, target)


def ensure_source_transition(current: str, target: str) -> None:
    _ensure_transition(current, target, _SOURCE_TRANSITIONS)


def ensure_parse_transition(current: str, target: str) -> None:
    _ensure_transition(current, target, _PARSE_TRANSITIONS)


def _source_values(value: NewAcceptedSource) -> dict[str, Any]:
    return {
        "id": value.source_document_id,
        "tenant_id": value.scope.tenant_id,
        "project_id": value.scope.project_id,
        "upload_request_id": value.upload_request_id,
        "authorization_provenance": value.authorization_provenance,
        "authorization_sha256": value.authorization_sha256,
        "original_display_name": value.original_display_name,
        "declared_media_type": value.declared_media_type,
        "detected_media_type": value.detected_media_type,
        "byte_count": value.byte_count,
        "content_sha256": value.content_sha256,
        "object_key": value.object_key,
        "status": "accepted",
        "aggregate_version": 1,
        "failure_code": None,
        "accepted_at": value.accepted_at,
    }


def _source_matches(existing: SourceDocument, value: NewAcceptedSource) -> bool:
    expected = _source_values(value)
    identity_fields = (
        "tenant_id",
        "project_id",
        "upload_request_id",
        "authorization_provenance",
        "authorization_sha256",
        "original_display_name",
        "declared_media_type",
        "detected_media_type",
        "byte_count",
        "content_sha256",
        "object_key",
    )
    return all(getattr(existing, key) == expected[key] for key in identity_fields)


def _parse_values(value: NewParseRun) -> dict[str, Any]:
    return {
        "id": value.parse_run_id,
        "tenant_id": value.scope.tenant_id,
        "project_id": value.scope.project_id,
        "source_document_id": value.source_document_id,
        "source_content_sha256": value.source_content_sha256,
        "parser_name": value.profile.parser_name,
        "parser_version": value.profile.parser_version,
        "parser_config_sha256": value.profile.configuration_sha256,
        "chunker_version": value.profile.chunker_version,
        "expected_chunker_name": value.profile.chunker_name,
        "expected_chunker_config_sha256": value.profile.chunker_configuration_sha256,
        "expected_containment_policy": value.profile.containment_policy,
        "identity_schema_version": 1,
        "expected_execution_config_sha256": value.expected_execution_configuration_sha256,
        "status": "pending",
        "failure_code": None,
    }


def _parse_matches(existing: SourceParseRun, value: NewParseRun) -> bool:
    expected = _parse_values(value)
    identity_fields = (
        "tenant_id",
        "project_id",
        "source_document_id",
        "source_content_sha256",
        "parser_name",
        "parser_version",
        "parser_config_sha256",
        "chunker_version",
        "expected_chunker_name",
        "expected_chunker_config_sha256",
        "expected_containment_policy",
        "identity_schema_version",
        "expected_execution_config_sha256",
    )
    return all(getattr(existing, key) == expected[key] for key in identity_fields)


class SqlAlchemySourceRepository:
    """Authority repository; transaction ownership always remains with its caller."""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create_accepted(self, value: NewAcceptedSource) -> CreateResult[SourceDocument]:
        statement = (
            insert(SourceDocument)
            .values(**_source_values(value))
            .on_conflict_do_nothing()
            .returning(SourceDocument)
        )
        created = cast(SourceDocument | None, await self._session.scalar(statement))
        if created is not None:
            return CreateResult(created, True)

        candidates = list(
            await self._session.scalars(
                select(SourceDocument).where(
                    or_(
                        SourceDocument.id == value.source_document_id,
                        (
                            (SourceDocument.tenant_id == value.scope.tenant_id)
                            & (SourceDocument.project_id == value.scope.project_id)
                            & (
                                (SourceDocument.upload_request_id == value.upload_request_id)
                                | (SourceDocument.content_sha256 == value.content_sha256)
                            )
                        ),
                        SourceDocument.object_key == value.object_key,
                    )
                )
            )
        )
        if len(candidates) != 1 or not _source_matches(candidates[0], value):
            raise SourceConflictError("upload request metadata conflicts with existing source")
        return CreateResult(candidates[0], False)

    async def create_parse_run(self, value: NewParseRun) -> CreateResult[SourceParseRun]:
        statement = (
            insert(SourceParseRun)
            .values(**_parse_values(value))
            .on_conflict_do_nothing()
            .returning(SourceParseRun)
        )
        created = cast(SourceParseRun | None, await self._session.scalar(statement))
        if created is not None:
            return CreateResult(created, True)
        candidates = list(
            await self._session.scalars(
                select(SourceParseRun).where(
                    or_(
                        SourceParseRun.id == value.parse_run_id,
                        (
                            (SourceParseRun.tenant_id == value.scope.tenant_id)
                            & (SourceParseRun.project_id == value.scope.project_id)
                            & (SourceParseRun.source_document_id == value.source_document_id)
                            & (SourceParseRun.source_content_sha256 == value.source_content_sha256)
                            & (SourceParseRun.parser_name == value.profile.parser_name)
                            & (SourceParseRun.parser_version == value.profile.parser_version)
                            & (
                                SourceParseRun.parser_config_sha256
                                == value.profile.configuration_sha256
                            )
                            & (SourceParseRun.chunker_version == value.profile.chunker_version)
                            & (SourceParseRun.expected_chunker_name == value.profile.chunker_name)
                            & (
                                SourceParseRun.expected_chunker_config_sha256
                                == value.profile.chunker_configuration_sha256
                            )
                            & (
                                SourceParseRun.expected_containment_policy
                                == value.profile.containment_policy
                            )
                        ),
                    )
                )
            )
        )
        if len(candidates) != 1 or not _parse_matches(candidates[0], value):
            raise SourceConflictError("parse request metadata conflicts with existing run")
        return CreateResult(candidates[0], False)

    async def begin_parse(self, value: NewParseRun) -> CreateResult[SourceParseRun]:
        source = cast(
            SourceDocument | None,
            await self._session.scalar(
                select(SourceDocument)
                .where(
                    SourceDocument.tenant_id == value.scope.tenant_id,
                    SourceDocument.project_id == value.scope.project_id,
                    SourceDocument.id == value.source_document_id,
                    SourceDocument.content_sha256 == value.source_content_sha256,
                )
                .with_for_update()
            ),
        )
        if source is None:
            raise SourceConflictError("source scope or content identity conflicts")
        if source.active_parse_run_id is not None:
            active = cast(
                SourceParseRun | None,
                await self._session.scalar(
                    select(SourceParseRun).where(
                        SourceParseRun.tenant_id == value.scope.tenant_id,
                        SourceParseRun.project_id == value.scope.project_id,
                        SourceParseRun.id == source.active_parse_run_id,
                        SourceParseRun.source_document_id == value.source_document_id,
                    )
                ),
            )
            if active is not None and _parse_matches(active, value):
                return CreateResult(active, False)
            if source.status != "chunks_ready":
                raise SourceConflictError("source already has a different active parse profile")
        if source.status not in {"accepted", "chunks_ready"}:
            raise SourceStateTransitionError(source.status, "parsing")
        parse_result = await self.create_parse_run(value)
        running = await self.transition_parse_run(
            value.scope,
            parse_result.value.id,
            "pending",
            "running",
            source_document_id=value.source_document_id,
            source_content_sha256=value.source_content_sha256,
            profile=value.profile,
            occurred_at=value.started_at,
        )
        if source.status == "chunks_ready":
            return CreateResult(running.value, parse_result.created)
        claimed = cast(
            SourceDocument | None,
            await self._session.scalar(
                update(SourceDocument)
                .where(
                    SourceDocument.tenant_id == value.scope.tenant_id,
                    SourceDocument.project_id == value.scope.project_id,
                    SourceDocument.id == value.source_document_id,
                    SourceDocument.status == "accepted",
                    SourceDocument.active_parse_run_id.is_(None),
                )
                .values(
                    status="parsing",
                    active_parse_run_id=running.value.id,
                    aggregate_version=1,
                )
                .returning(SourceDocument)
            ),
        )
        if claimed is None:
            raise SourceConflictError("source active parse lease changed concurrently")
        return CreateResult(running.value, True)

    async def persist_parse_product(self, value: PersistParseProduct) -> PersistProductResult:
        source = cast(
            SourceDocument | None,
            await self._session.scalar(authoritative_source_statement(value)),
        )
        if source is None:
            raise SourceConflictError("source does not exist in the requested scope")
        parse_run = cast(
            SourceParseRun | None,
            await self._session.scalar(authoritative_product_statement(value)),
        )
        if parse_run is None:
            raise SourceConflictError("parse product authority does not match source scope")
        product_sha256 = canonical_product_sha256(value.product)

        if parse_run.product_version is not None:
            if parse_run.status not in {"succeeded", "superseded"}:
                raise SourceConflictError("parse product version conflicts with lifecycle state")
            expected_identity = (
                value.product.identity.name,
                value.product.identity.configuration_sha256,
                value.product.identity.containment_policy,
            )
            actual_identity = (
                parse_run.chunker_name,
                parse_run.chunker_config_sha256,
                parse_run.containment_policy,
            )
            if actual_identity != expected_identity:
                raise SourceConflictError("persisted chunker identity conflicts with retry")
            if parse_run.product_sha256 != product_sha256:
                raise SourceConflictError("persisted parse product digest conflicts with retry")
            section_count = cast(
                int,
                await self._session.scalar(
                    select(func.count())
                    .select_from(SourceSection)
                    .where(
                        SourceSection.tenant_id == value.scope.tenant_id,
                        SourceSection.project_id == value.scope.project_id,
                        SourceSection.parse_run_id == value.parse_run_id,
                    )
                ),
            )
            chunk_count = cast(
                int,
                await self._session.scalar(
                    select(func.count())
                    .select_from(SourceChunk)
                    .where(
                        SourceChunk.tenant_id == value.scope.tenant_id,
                        SourceChunk.project_id == value.scope.project_id,
                        SourceChunk.parse_run_id == value.parse_run_id,
                    )
                ),
            )
            if (section_count, chunk_count) != (
                len(value.product.sections),
                len(value.product.chunks),
            ):
                raise SourceConflictError("persisted parse product counts conflict with retry")
            return PersistProductResult(source, parse_run, parse_run.product_version, False)

        if parse_run.status != "running":
            raise SourceStateTransitionError(parse_run.status, "succeeded")
        initial_parse = source.status == "parsing" and source.active_parse_run_id == parse_run.id
        reparse = source.status == "chunks_ready" and source.active_parse_run_id != parse_run.id
        if not (initial_parse or reparse):
            raise SourceConflictError("source active parse lineage changed concurrently")

        sections = section_row_values(value)
        chunks = chunk_row_values(value)
        if not sections:
            raise ValueError("parse product must contain at least one section")
        for batch in bounded_batches(sections, _SECTION_INSERT_BATCH):
            await self._session.execute(insert(SourceSection).values(batch))
        for batch in bounded_batches(chunks, _CHUNK_INSERT_BATCH):
            await self._session.execute(insert(SourceChunk).values(batch))

        product_version = 3 if source.aggregate_version < 3 else source.aggregate_version + 1
        completed = cast(
            SourceParseRun | None,
            await self._session.scalar(
                update(SourceParseRun)
                .where(
                    SourceParseRun.tenant_id == value.scope.tenant_id,
                    SourceParseRun.project_id == value.scope.project_id,
                    SourceParseRun.id == value.parse_run_id,
                    SourceParseRun.status == "running",
                    SourceParseRun.product_version.is_(None),
                )
                .values(
                    status="succeeded",
                    completed_at=value.occurred_at,
                    product_version=product_version,
                    chunker_name=value.product.identity.name,
                    chunker_config_sha256=value.product.identity.configuration_sha256,
                    containment_policy=value.product.identity.containment_policy,
                    product_sha256=product_sha256,
                )
                .returning(SourceParseRun)
            ),
        )
        if completed is None:
            raise SourceConflictError("parse product was completed concurrently")
        previous_id = source.active_parse_run_id
        if previous_id is not None and previous_id != parse_run.id:
            await self._session.execute(
                update(SourceParseRun)
                .where(
                    SourceParseRun.tenant_id == value.scope.tenant_id,
                    SourceParseRun.project_id == value.scope.project_id,
                    SourceParseRun.id == previous_id,
                    SourceParseRun.source_document_id == value.source_document_id,
                    SourceParseRun.status == "succeeded",
                )
                .values(status="superseded")
            )
        promoted = cast(
            SourceDocument | None,
            await self._session.scalar(
                update(SourceDocument)
                .where(
                    SourceDocument.tenant_id == value.scope.tenant_id,
                    SourceDocument.project_id == value.scope.project_id,
                    SourceDocument.id == value.source_document_id,
                    SourceDocument.content_sha256 == value.source_content_sha256,
                    SourceDocument.aggregate_version == source.aggregate_version,
                )
                .values(
                    status="chunks_ready",
                    failure_code=None,
                    active_parse_run_id=parse_run.id,
                    aggregate_version=product_version,
                )
                .returning(SourceDocument)
            ),
        )
        if promoted is None:
            raise SourceConflictError("source product version changed concurrently")
        return PersistProductResult(promoted, completed, product_version, True)

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
    ) -> CreateResult[SourceDocument]:
        ensure_source_transition(expected_status, target_status)
        lineage_values = (parse_run_id, source_content_sha256, profile, required_parse_status)
        if any(value is not None for value in lineage_values) and not all(
            value is not None for value in lineage_values
        ):
            raise ValueError("complete parse lineage is required")
        conditions = [
            SourceDocument.tenant_id == scope.tenant_id,
            SourceDocument.project_id == scope.project_id,
            SourceDocument.id == source_document_id,
        ]
        if parse_run_id is not None and source_content_sha256 is not None:
            assert profile is not None and required_parse_status is not None
            conditions.append(SourceDocument.content_sha256 == source_content_sha256)
            conditions.append(SourceDocument.active_parse_run_id == parse_run_id)
            conditions.append(
                exists(
                    select(1).where(
                        SourceParseRun.tenant_id == scope.tenant_id,
                        SourceParseRun.project_id == scope.project_id,
                        SourceParseRun.id == parse_run_id,
                        SourceParseRun.source_document_id == source_document_id,
                        SourceParseRun.source_content_sha256 == source_content_sha256,
                        SourceParseRun.parser_name == profile.parser_name,
                        SourceParseRun.parser_version == profile.parser_version,
                        SourceParseRun.parser_config_sha256 == profile.configuration_sha256,
                        SourceParseRun.chunker_version == profile.chunker_version,
                        SourceParseRun.expected_chunker_name == profile.chunker_name,
                        SourceParseRun.expected_chunker_config_sha256
                        == profile.chunker_configuration_sha256,
                        SourceParseRun.expected_containment_policy == profile.containment_policy,
                        SourceParseRun.status == required_parse_status,
                    )
                )
            )
        if expected_status == target_status:
            existing = cast(
                SourceDocument | None,
                await self._session.scalar(select(SourceDocument).where(*conditions)),
            )
            if existing is None:
                raise SourceConflictError("source and parse lineage do not match")
            if existing.status == target_status and existing.failure_code == failure_code:
                return CreateResult(existing, False)
            if existing.status == target_status:
                raise SourceConflictError("source state metadata conflicts")
            raise SourceStateTransitionError(existing.status, target_status)
        aggregate_version = {
            "parsed": 2,
            "chunks_ready": 3,
            "failed": 3 if expected_status == "parsed" else 2,
        }.get(target_status)
        values: dict[str, Any] = {"status": target_status, "failure_code": failure_code}
        if aggregate_version is not None:
            values["aggregate_version"] = aggregate_version
        changed = cast(
            SourceDocument | None,
            await self._session.scalar(
                update(SourceDocument)
                .where(
                    *conditions,
                    SourceDocument.status == expected_status,
                )
                .values(**values)
                .returning(SourceDocument)
            ),
        )
        if changed is not None:
            return CreateResult(changed, expected_status != target_status)
        existing = cast(
            SourceDocument | None,
            await self._session.scalar(select(SourceDocument).where(*conditions)),
        )
        if existing is None:
            raise SourceConflictError("source does not exist in the requested scope")
        if existing.status == target_status and existing.failure_code == failure_code:
            return CreateResult(existing, False)
        raise SourceStateTransitionError(existing.status, target_status)

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
    ) -> CreateResult[SourceParseRun]:
        ensure_parse_transition(expected_status, target_status)
        conditions = [
            SourceParseRun.tenant_id == scope.tenant_id,
            SourceParseRun.project_id == scope.project_id,
            SourceParseRun.id == parse_run_id,
            SourceParseRun.source_document_id == source_document_id,
            SourceParseRun.source_content_sha256 == source_content_sha256,
            SourceParseRun.parser_name == profile.parser_name,
            SourceParseRun.parser_version == profile.parser_version,
            SourceParseRun.parser_config_sha256 == profile.configuration_sha256,
            SourceParseRun.chunker_version == profile.chunker_version,
            SourceParseRun.expected_chunker_name == profile.chunker_name,
            SourceParseRun.expected_chunker_config_sha256 == profile.chunker_configuration_sha256,
            SourceParseRun.expected_containment_policy == profile.containment_policy,
        ]
        if expected_status == target_status:
            existing = cast(
                SourceParseRun | None,
                await self._session.scalar(select(SourceParseRun).where(*conditions)),
            )
            if existing is None:
                raise SourceConflictError("parse lineage conflicts with source")
            if existing.status == target_status and existing.failure_code == failure_code:
                return CreateResult(existing, False)
            if existing.status == target_status:
                raise SourceConflictError("parse state metadata conflicts")
            raise SourceStateTransitionError(existing.status, target_status)
        timestamps: dict[str, datetime] = {}
        if target_status == "running":
            timestamps["started_at"] = occurred_at
        if target_status in {"succeeded", "failed", "superseded"}:
            timestamps["completed_at"] = occurred_at
        changed = cast(
            SourceParseRun | None,
            await self._session.scalar(
                update(SourceParseRun)
                .where(
                    *conditions,
                    SourceParseRun.status == expected_status,
                )
                .values(status=target_status, failure_code=failure_code, **timestamps)
                .returning(SourceParseRun)
            ),
        )
        if changed is not None:
            return CreateResult(changed, expected_status != target_status)
        existing = cast(
            SourceParseRun | None,
            await self._session.scalar(select(SourceParseRun).where(*conditions)),
        )
        if existing is None:
            raise SourceConflictError("parse lineage conflicts with source")
        if existing.status == target_status and existing.failure_code == failure_code:
            return CreateResult(existing, False)
        raise SourceStateTransitionError(existing.status, target_status)


def find_source_by_content_statement(
    tenant_id: uuid.UUID, project_id: uuid.UUID, content_sha256: str
) -> Select[tuple[SourceDocument]]:
    return select(SourceDocument).where(
        SourceDocument.tenant_id == tenant_id,
        SourceDocument.project_id == project_id,
        SourceDocument.content_sha256 == content_sha256,
    )


def find_parse_run_statement(
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    source_document_id: uuid.UUID,
    source_content_sha256: str,
    parser_name: str,
    parser_version: str,
    parser_config_sha256: str,
    chunker_version: str,
) -> Select[tuple[SourceParseRun]]:
    return select(SourceParseRun).where(
        SourceParseRun.tenant_id == tenant_id,
        SourceParseRun.project_id == project_id,
        SourceParseRun.source_document_id == source_document_id,
        SourceParseRun.source_content_sha256 == source_content_sha256,
        SourceParseRun.parser_name == parser_name,
        SourceParseRun.parser_version == parser_version,
        SourceParseRun.parser_config_sha256 == parser_config_sha256,
        SourceParseRun.chunker_version == chunker_version,
    )


def checkpoint_cas_statement(
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    projection_name: str,
    checkpoint_key: str,
    projection_version: int,
    event_id: uuid.UUID,
    event_occurred_at: datetime,
    updated_at: datetime,
) -> ReturningInsert[tuple[ProjectionCheckpoint]]:
    statement = insert(ProjectionCheckpoint).values(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        project_id=project_id,
        projection_name=projection_name,
        checkpoint_key=checkpoint_key,
        projection_version=projection_version,
        status="current",
        last_applied_event_id=event_id,
        last_applied_event_occurred_at=event_occurred_at,
        attempted_version=projection_version,
        last_attempted_event_id=event_id,
        failure_code=None,
        updated_at=updated_at,
    )
    is_newer = statement.excluded.projection_version > ProjectionCheckpoint.projection_version
    is_latest_attempt = (
        statement.excluded.projection_version >= ProjectionCheckpoint.attempted_version
    )
    matches_attempt = (
        statement.excluded.projection_version > ProjectionCheckpoint.attempted_version
    ) | (
        (statement.excluded.projection_version == ProjectionCheckpoint.attempted_version)
        & (
            statement.excluded.last_attempted_event_id
            == ProjectionCheckpoint.last_attempted_event_id
        )
    )
    return statement.on_conflict_do_update(
        constraint="uq_projection_ckpts_scope_name_key",
        set_={
            "projection_version": case(
                (is_newer, statement.excluded.projection_version),
                else_=ProjectionCheckpoint.projection_version,
            ),
            "status": case(
                (is_newer & is_latest_attempt, "current"),
                else_=ProjectionCheckpoint.status,
            ),
            "last_applied_event_id": case(
                (is_newer, statement.excluded.last_applied_event_id),
                else_=ProjectionCheckpoint.last_applied_event_id,
            ),
            "last_applied_event_occurred_at": case(
                (is_newer, statement.excluded.last_applied_event_occurred_at),
                else_=ProjectionCheckpoint.last_applied_event_occurred_at,
            ),
            "attempted_version": case(
                (
                    is_newer,
                    func.greatest(
                        ProjectionCheckpoint.attempted_version,
                        statement.excluded.attempted_version,
                    ),
                ),
                else_=ProjectionCheckpoint.attempted_version,
            ),
            "last_attempted_event_id": case(
                (is_newer & is_latest_attempt, statement.excluded.last_attempted_event_id),
                else_=ProjectionCheckpoint.last_attempted_event_id,
            ),
            "failure_code": case(
                (is_newer & is_latest_attempt, None),
                else_=ProjectionCheckpoint.failure_code,
            ),
            "updated_at": func.greatest(
                ProjectionCheckpoint.updated_at, statement.excluded.updated_at
            ),
        },
        where=(is_newer & matches_attempt)
        | (
            (statement.excluded.projection_version == ProjectionCheckpoint.projection_version)
            & (
                statement.excluded.last_applied_event_id
                == ProjectionCheckpoint.last_applied_event_id
            )
        ),
    ).returning(ProjectionCheckpoint)


def checkpoint_failure_statement(
    *,
    tenant_id: uuid.UUID,
    project_id: uuid.UUID,
    projection_name: str,
    checkpoint_key: str,
    attempted_version: int,
    event_id: uuid.UUID,
    failure_code: str,
    updated_at: datetime,
) -> ReturningInsert[tuple[ProjectionCheckpoint]]:
    statement = insert(ProjectionCheckpoint).values(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        project_id=project_id,
        projection_name=projection_name,
        checkpoint_key=checkpoint_key,
        projection_version=0,
        status="failed",
        last_applied_event_id=None,
        last_applied_event_occurred_at=None,
        attempted_version=attempted_version,
        last_attempted_event_id=event_id,
        failure_code=failure_code,
        updated_at=updated_at,
    )
    return statement.on_conflict_do_update(
        constraint="uq_projection_ckpts_scope_name_key",
        set_={
            "status": "failed",
            "attempted_version": statement.excluded.attempted_version,
            "last_attempted_event_id": statement.excluded.last_attempted_event_id,
            "failure_code": statement.excluded.failure_code,
            "updated_at": func.greatest(
                ProjectionCheckpoint.updated_at, statement.excluded.updated_at
            ),
        },
        where=(statement.excluded.attempted_version > ProjectionCheckpoint.projection_version)
        & (
            (statement.excluded.attempted_version > ProjectionCheckpoint.attempted_version)
            | (
                (statement.excluded.attempted_version == ProjectionCheckpoint.attempted_version)
                & (
                    statement.excluded.last_attempted_event_id
                    == ProjectionCheckpoint.last_attempted_event_id
                )
                & (ProjectionCheckpoint.status == "failed")
            )
        ),
    ).returning(ProjectionCheckpoint)


class ProjectionCheckpointRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def advance(
        self,
        *,
        tenant_id: uuid.UUID,
        project_id: uuid.UUID,
        projection_name: str,
        checkpoint_key: str,
        projection_version: int,
        event_id: uuid.UUID,
        event_occurred_at: datetime,
        updated_at: datetime,
    ) -> ProjectionCheckpoint | None:
        return cast(
            ProjectionCheckpoint | None,
            await self._session.scalar(
                checkpoint_cas_statement(
                    tenant_id=tenant_id,
                    project_id=project_id,
                    projection_name=projection_name,
                    checkpoint_key=checkpoint_key,
                    projection_version=projection_version,
                    event_id=event_id,
                    event_occurred_at=event_occurred_at,
                    updated_at=updated_at,
                )
            ),
        )

    async def mark_failure(
        self,
        *,
        tenant_id: uuid.UUID,
        project_id: uuid.UUID,
        projection_name: str,
        checkpoint_key: str,
        attempted_version: int,
        event_id: uuid.UUID,
        failure_code: str,
        updated_at: datetime,
    ) -> ProjectionCheckpoint | None:
        return cast(
            ProjectionCheckpoint | None,
            await self._session.scalar(
                checkpoint_failure_statement(
                    tenant_id=tenant_id,
                    project_id=project_id,
                    projection_name=projection_name,
                    checkpoint_key=checkpoint_key,
                    attempted_version=attempted_version,
                    event_id=event_id,
                    failure_code=failure_code,
                    updated_at=updated_at,
                )
            ),
        )


__all__ = [
    "CreateResult",
    "NewAcceptedSource",
    "NewParseRun",
    "ProjectionCheckpointRepository",
    "SqlAlchemySourceRepository",
    "checkpoint_cas_statement",
    "checkpoint_failure_statement",
    "find_parse_run_statement",
    "find_source_by_content_statement",
    "ensure_parse_transition",
    "ensure_source_transition",
    "authoritative_product_statement",
    "authoritative_source_statement",
    "bounded_batches",
    "chunk_row_values",
    "PersistParseProduct",
    "PersistProductResult",
    "section_row_values",
]
