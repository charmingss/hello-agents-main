"""Persist server-generated candidates and record human decisions, never world facts."""

import base64
import json
from copy import deepcopy
from datetime import UTC, datetime
from typing import Annotated, Any, Literal
from uuid import UUID, uuid4

from pydantic import Field, model_validator
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novel_agent.analysis.contracts import AnalysisError, Name, ShortText, StrictModel
from novel_agent.analysis.service import SourceAnalysisService
from novel_agent.db.models.analysis import SourceAnalysisRecord, SourceAnalysisReview
from novel_agent.db.models.project import Project
from novel_agent.db.models.source import SourceDocument, SourceParseRun
from novel_agent.vector.schema import TenantProjectScope

MAX_HISTORY_CHARACTERS = 500


class CharacterReviewPatch(StrictModel):
    expected_revision: Annotated[int, Field(strict=True, ge=0)]
    decision: Literal["pending", "accepted", "rejected"]
    name: Name | None = None
    aliases: Annotated[list[Name], Field(max_length=10)] | None = None
    description: Annotated[list[ShortText], Field(max_length=10)] | None = None
    traits: Annotated[list[ShortText], Field(max_length=10)] | None = None

    @model_validator(mode="after")
    def reject_explicit_null(self) -> "CharacterReviewPatch":
        if any(getattr(self, key) is None for key in self.model_fields_set):
            raise ValueError("omit unchanged fields instead of null")
        return self


def is_stale(
    source: SourceDocument, run: SourceParseRun | None, record: SourceAnalysisRecord
) -> bool:
    return (
        source.status != "chunks_ready"
        or source.active_parse_run_id != record.parse_run_id
        or run is None
        or run.id != record.parse_run_id
        or run.status != "succeeded"
        or run.product_version != record.product_version
        or source.aggregate_version != record.product_version
        or run.product_sha256 != record.product_sha256
    )


def detail(
    record: SourceAnalysisRecord, review: SourceAnalysisReview, stale: bool
) -> dict[str, Any]:
    return {
        "analysis_id": str(record.id),
        "source_document_id": str(record.source_document_id),
        "original": deepcopy(record.original),
        "character_ids": list(record.character_ids),
        "revision": review.revision,
        "review": deepcopy(review.snapshot),
        "audit": {"subject": review.subject, "created_at": review.created_at.isoformat()},
        "created_by_subject": record.created_by_subject,
        "created_at": record.created_at.isoformat(),
        "stale": stale,
    }


def _encode_cursor(created_at: datetime, analysis_id: UUID) -> str:
    payload = json.dumps(
        {"created_at": created_at.isoformat(), "analysis_id": str(analysis_id)},
        separators=(",", ":"),
    ).encode()
    return base64.urlsafe_b64encode(payload).rstrip(b"=").decode()


def _decode_cursor(value: str) -> tuple[datetime, UUID]:
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        payload = json.loads(raw)
        if not isinstance(payload, dict) or set(payload) != {"created_at", "analysis_id"}:
            raise ValueError
        created_at = datetime.fromisoformat(payload["created_at"])
        if created_at.tzinfo is None:
            raise ValueError
        return created_at, UUID(payload["analysis_id"])
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        raise AnalysisError("invalid_analysis_cursor", 422) from None


class SourceAnalysisHistoryService:
    def __init__(
        self, preview: SourceAnalysisService | None,
        session_factory: async_sessionmaker[AsyncSession],
    ) -> None:
        self._preview = preview
        self._sessions = session_factory

    async def _source(
        self, session: AsyncSession, scope: TenantProjectScope, source_id: UUID,
        *, write: bool,
    ) -> tuple[SourceDocument, SourceParseRun | None]:
        # All operations lock in project -> source -> run -> record order. SHARE locks
        # permit concurrent reads and prevent deletion/status changes until the short tx ends.
        project = await session.execute(
            select(Project).where(Project.tenant_id == scope.tenant_id,
                                  Project.id == scope.project_id, Project.status == "active")
            .with_for_update(read=True)
        )
        if project.scalar_one_or_none() is None:
            raise AnalysisError("source_not_found", 404)
        result = await session.execute(
            select(SourceDocument).where(SourceDocument.tenant_id == scope.tenant_id,
                                         SourceDocument.project_id == scope.project_id,
                                         SourceDocument.id == source_id)
            .with_for_update(read=not write)
        )
        source = result.scalar_one_or_none()
        if source is None:
            raise AnalysisError("source_not_found", 404)
        parse_result = await session.execute(
            select(SourceParseRun).where(SourceParseRun.tenant_id == scope.tenant_id,
                                         SourceParseRun.project_id == scope.project_id,
                                         SourceParseRun.source_document_id == source_id,
                                         SourceParseRun.id == source.active_parse_run_id)
            .with_for_update(read=not write)
        )
        return source, parse_result.scalar_one_or_none()

    async def _record(
        self, session: AsyncSession, scope: TenantProjectScope, source_id: UUID,
        analysis_id: UUID, *, write: bool,
    ) -> SourceAnalysisRecord:
        result = await session.execute(
            select(SourceAnalysisRecord).where(
                SourceAnalysisRecord.tenant_id == scope.tenant_id,
                SourceAnalysisRecord.project_id == scope.project_id,
                SourceAnalysisRecord.source_document_id == source_id,
                SourceAnalysisRecord.id == analysis_id,
            ).with_for_update(read=not write)
        )
        record = result.scalar_one_or_none()
        if record is None:
            raise AnalysisError("analysis_not_found", 404)
        return record

    async def _head(
        self, session: AsyncSession, record: SourceAnalysisRecord
    ) -> SourceAnalysisReview:
        result = await session.execute(select(SourceAnalysisReview).where(
            SourceAnalysisReview.tenant_id == record.tenant_id,
            SourceAnalysisReview.project_id == record.project_id,
            SourceAnalysisReview.source_document_id == record.source_document_id,
            SourceAnalysisReview.analysis_id == record.id,
            SourceAnalysisReview.revision == record.current_revision,
        ))
        head = result.scalar_one_or_none()
        if head is None:
            raise AnalysisError("analysis_unavailable", 503)
        return head

    async def create(
        self, scope: TenantProjectScope, source_id: UUID, subject: str
    ) -> dict[str, Any]:
        if self._preview is None:
            raise AnalysisError("analysis_unavailable", 503)
        # The existing preview service authorizes before invoking the model and closes
        # its read session first. No history transaction spans provider generation.
        original = await self._preview.preview(scope, source_id)
        return await self.append_validated(scope, source_id, subject, original)

    async def append_validated(
        self,
        scope: TenantProjectScope,
        source_id: UUID,
        subject: str,
        original: dict[str, Any],
    ) -> dict[str, Any]:
        """Append an already validated candidate after rechecking its source lineage."""
        async with self._sessions() as session, session.begin():
            source, run = await self._source(session, scope, source_id, write=True)
            return await self.append_validated_in_session(
                session,
                scope,
                source_id,
                subject,
                original,
                source=source,
                run=run,
            )

    async def append_validated_in_session(
        self,
        session: AsyncSession,
        scope: TenantProjectScope,
        source_id: UUID,
        subject: str,
        original: dict[str, Any],
        *,
        source: SourceDocument,
        run: SourceParseRun | None,
        character_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        """Append record and revision zero inside the caller's active transaction."""
        characters = original.get("characters")
        if not isinstance(characters, list):
            raise AnalysisError("analysis_invalid_output", 502)
        if len(characters) > MAX_HISTORY_CHARACTERS:
            raise AnalysisError("analysis_result_too_large", 422)
        ids = character_ids or [str(uuid4()) for _ in characters]
        if len(ids) != len(characters):
            raise AnalysisError("analysis_invalid_output", 502)
        if any(str(UUID(value)) != value for value in ids):
            raise AnalysisError("analysis_invalid_output", 502)
        now = datetime.now(UTC)
        record = SourceAnalysisRecord(
            id=uuid4(), tenant_id=scope.tenant_id, project_id=scope.project_id,
            source_document_id=source_id, parse_run_id=UUID(original["parse_run_id"]),
            product_version=original["product_version"], product_sha256=original["product_sha256"],
            provider=original["provider"], model=original["model"],
            prompt_version=original["prompt_version"], original=deepcopy(original),
            character_ids=list(ids), current_revision=0,
            created_by_subject=subject, created_at=now,
        )
        snapshot = {"characters": [
            {"character_id": cid, "original_index": index, "decision": "pending",
             "name": character["name"], "aliases": list(character["aliases"]),
             "description": [claim["text"] for claim in character["description"]],
             "traits": [claim["text"] for claim in character["traits"]],
             "text_origin": "model", "edited_fields": []}
            for index, (cid, character) in enumerate(
                zip(record.character_ids, characters, strict=True))
        ]}
        review = self._revision(record, snapshot, subject, now, 0)
        if is_stale(source, run, record):
            raise AnalysisError("source_changed", 409)
        session.add(record)
        await session.flush()
        session.add(review)
        await session.flush()
        return detail(record, review, False)

    @staticmethod
    def _revision(
        record: SourceAnalysisRecord, snapshot: dict[str, Any], subject: str,
        now: datetime, revision: int,
    ) -> SourceAnalysisReview:
        return SourceAnalysisReview(
            id=uuid4(), tenant_id=record.tenant_id, project_id=record.project_id,
            source_document_id=record.source_document_id, analysis_id=record.id,
            revision=revision, snapshot=deepcopy(snapshot), subject=subject, created_at=now,
        )

    async def get(
        self, scope: TenantProjectScope, source_id: UUID, analysis_id: UUID
    ) -> dict[str, Any]:
        async with self._sessions() as session, session.begin():
            source, run = await self._source(session, scope, source_id, write=False)
            record = await self._record(session, scope, source_id, analysis_id, write=False)
            head = await self._head(session, record)
            return detail(record, head, is_stale(source, run, record))

    async def list(
        self, scope: TenantProjectScope, source_id: UUID, *, limit: int, cursor: str | None
    ) -> dict[str, Any]:
        boundary = _decode_cursor(cursor) if cursor is not None else None
        async with self._sessions() as session, session.begin():
            source, run = await self._source(session, scope, source_id, write=False)
            statement = select(SourceAnalysisRecord).where(
                SourceAnalysisRecord.tenant_id == scope.tenant_id,
                SourceAnalysisRecord.project_id == scope.project_id,
                SourceAnalysisRecord.source_document_id == source_id,
            )
            if boundary is not None:
                created_at, analysis_id = boundary
                statement = statement.where(
                    or_(
                        SourceAnalysisRecord.created_at < created_at,
                        and_(
                            SourceAnalysisRecord.created_at == created_at,
                            SourceAnalysisRecord.id < analysis_id,
                        ),
                    )
                )
            result = await session.execute(
                statement.order_by(
                    SourceAnalysisRecord.created_at.desc(), SourceAnalysisRecord.id.desc()
                ).limit(limit + 1)
            )
            records = list(result.scalars().all())
            has_more = len(records) > limit
            records = records[:limit]
            items = [
                {
                    "analysis_id": str(record.id),
                    "source_document_id": str(record.source_document_id),
                    "revision": record.current_revision,
                    "created_by_subject": record.created_by_subject,
                    "created_at": record.created_at.isoformat(),
                    "stale": is_stale(source, run, record),
                }
                for record in records
            ]
            return {
                "items": items,
                "limit": limit,
                "has_more": has_more,
                "next_cursor": (
                    _encode_cursor(records[-1].created_at, records[-1].id)
                    if has_more and records
                    else None
                ),
            }

    async def review(
        self, scope: TenantProjectScope, source_id: UUID, analysis_id: UUID,
        character_id: UUID, patch: CharacterReviewPatch, subject: str,
    ) -> dict[str, Any]:
        async with self._sessions() as session, session.begin():
            source, run = await self._source(session, scope, source_id, write=True)
            record = await self._record(session, scope, source_id, analysis_id, write=True)
            if is_stale(source, run, record):
                raise AnalysisError("analysis_stale", 409)
            if str(character_id) not in record.character_ids:
                raise AnalysisError("analysis_character_not_found", 404)
            if record.current_revision != patch.expected_revision:
                raise AnalysisError("analysis_revision_conflict", 409)
            head = await self._head(session, record)
            snapshot = deepcopy(head.snapshot)
            character = snapshot["characters"][record.character_ids.index(str(character_id))]
            edits = patch.model_dump(exclude_unset=True, exclude={"expected_revision", "decision"})
            character.update(edits)
            character["decision"] = patch.decision
            if edits:
                character["text_origin"] = "human"
                character["edited_fields"] = sorted(set(character["edited_fields"]) | edits.keys())
            revision = self._revision(record, snapshot, subject, datetime.now(UTC),
                                      record.current_revision + 1)
            session.add(revision)
            await session.flush()
            record.current_revision = revision.revision
            await session.flush()
            return detail(record, revision, False)
