"""SQL authority and fenced execution for resumable full-source analysis jobs."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novel_agent.analysis.batches import BatchPlan, plan_batches
from novel_agent.analysis.contracts import (
    MAX_CHARS,
    MAX_SECTIONS,
    PROMPT_VERSION,
    AnalysisError,
    AnalysisProvider,
    Section,
    Window,
)
from novel_agent.analysis.history import MAX_HISTORY_CHARACTERS, SourceAnalysisHistoryService
from novel_agent.analysis.merge import BatchCandidate, merge_batches
from novel_agent.analysis.service import validate_candidate
from novel_agent.db.models.analysis import FullAnalysisBatch, FullAnalysisJob
from novel_agent.db.models.project import Project
from novel_agent.db.models.source import SourceDocument, SourceParseRun, SourceSection
from novel_agent.vector.schema import TenantProjectScope


@dataclass(frozen=True)
class FullAnalysisJobInput:
    tenant_id: UUID
    project_id: UUID
    source_id: UUID
    job_id: UUID


@dataclass(frozen=True)
class BatchClaim:
    scope: TenantProjectScope
    source_id: UUID
    job_id: UUID
    batch_index: int
    attempt_token: UUID


@dataclass(frozen=True)
class FullAnalysisJobStatus:
    job_id: UUID
    status: str
    total_batches: int
    completed_batches: int
    failed_batches: int
    analysis_id: UUID | None
    error_code: str | None
    dispatch_pending: bool


class FullAnalysisWorkflowGateway(Protocol):
    async def dispatch(
        self, command: FullAnalysisJobInput, *, workflow_id: str
    ) -> None: ...


SessionFactory = async_sessionmaker[AsyncSession] | Callable[[], AsyncSession]

_BATCH_FAILURE_CODES = frozenset(
    {
        "analysis_input_too_large",
        "analysis_invalid_output",
        "analysis_unavailable",
        "source_changed",
    }
)


def full_analysis_workflow_id(job_id: UUID) -> str:
    return f"full-analysis:{job_id}"


class FullAnalysisJobService:
    def __init__(
        self,
        session_factory: SessionFactory,
        provider: AnalysisProvider,
        gateway: FullAnalysisWorkflowGateway,
        *,
        char_limit: int = MAX_CHARS,
        section_limit: int = MAX_SECTIONS,
    ) -> None:
        self._sessions = session_factory
        self._provider = provider
        self._gateway = gateway
        self._char_limit = char_limit
        self._section_limit = section_limit

    async def create(
        self, scope: TenantProjectScope, source_id: UUID, subject: str
    ) -> FullAnalysisJobStatus:
        now = datetime.now(UTC)
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=False)
            source = await self._source(session, scope, source_id, write=True)
            run = await self._parse_run(session, scope, source, write=True)
            self._require_ready(source, run)
            sections = await self._sections(session, scope, run.id, write=False)
            plans = plan_batches(
                [self._section(row) for row in sections],
                char_limit=self._char_limit,
                section_limit=self._section_limit,
            )
            if not plans:
                raise AnalysisError("analysis_input_too_large", 422)
            assert run.product_version is not None and run.product_sha256 is not None
            job_id = uuid4()
            job = FullAnalysisJob(
                id=job_id,
                tenant_id=scope.tenant_id,
                project_id=scope.project_id,
                source_document_id=source_id,
                parse_run_id=run.id,
                product_version=run.product_version,
                product_sha256=run.product_sha256,
                provider=self._provider.provider,
                model=self._provider.model,
                prompt_version=PROMPT_VERSION,
                status="queued",
                total_batches=len(plans),
                completed_batches=0,
                failed_batches=0,
                workflow_id=full_analysis_workflow_id(job_id),
                final_analysis_id=None,
                error_code=None,
                created_by_subject=subject,
                created_at=now,
                updated_at=now,
            )
            batches = [self._new_batch(scope, source_id, job, plan, now) for plan in plans]
            session.add(job)
            session.add_all(batches)
            await session.flush()

        await self._dispatch(job)
        return self._status(job)

    async def status(
        self, scope: TenantProjectScope, source_id: UUID, job_id: UUID
    ) -> FullAnalysisJobStatus:
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=False)
            await self._source(session, scope, source_id, write=False)
            job = await self._job(session, scope, source_id, job_id, write=False)
            return self._status(job)

    async def next_batch_index(
        self, scope: TenantProjectScope, source_id: UUID, job_id: UUID
    ) -> int | None:
        """Return the index of the next pending batch, or ``None`` if all done."""
        stale = False
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=False)
            source = await self._source(session, scope, source_id, write=True)
            run = await self._parse_run_optional(session, scope, source, write=True)
            job = await self._job(session, scope, source_id, job_id, write=True)
            if job.status in {"stale", "succeeded", "failed"}:
                raise AnalysisError("analysis_job_" + job.status, 409)
            if not self._lineage_matches(source, run, job):
                job.status = "stale"
                job.error_code = "source_changed"
                job.updated_at = datetime.now(UTC)
                stale = True
            else:
                batches = await self._batches(
                    session, scope, source_id, job_id, write=False
                )
                await session.flush()
                for batch in batches:
                    if batch.status == "pending":
                        return batch.batch_index
                return None
        if stale:
            raise AnalysisError("analysis_job_stale", 409)
        return None  # unreachable – keeps mypy happy

    async def claim_batch(
        self,
        scope: TenantProjectScope,
        source_id: UUID,
        job_id: UUID,
        *,
        batch_index: int,
    ) -> BatchClaim:
        stale = False
        claimed: BatchClaim | None = None
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=False)
            source = await self._source(session, scope, source_id, write=True)
            run = await self._parse_run_optional(session, scope, source, write=True)
            job = await self._job(session, scope, source_id, job_id, write=True)
            batch = await self._batch(
                session, scope, source_id, job_id, batch_index, write=True
            )
            if job.status == "stale":
                raise AnalysisError("analysis_job_stale", 409)
            if job.status == "succeeded":
                raise AnalysisError("analysis_job_conflict", 409)
            if not self._lineage_matches(source, run, job):
                job.status = "stale"
                job.error_code = "source_changed"
                job.updated_at = datetime.now(UTC)
                stale = True
            else:
                if job.status not in {"queued", "running"} or batch.status != "pending":
                    raise AnalysisError("analysis_job_conflict", 409)
                token = uuid4()
                batch.status = "running"
                batch.attempt_token = token
                batch.attempts += 1
                batch.error_code = None
                batch.updated_at = datetime.now(UTC)
                job.status = "running"
                job.error_code = None
                job.updated_at = batch.updated_at
                claimed = BatchClaim(scope, source_id, job_id, batch_index, token)
            await session.flush()
        if stale:
            raise AnalysisError("analysis_job_stale", 409)
        assert claimed is not None
        return claimed

    async def analyze_claim(self, claim: BatchClaim) -> dict[str, Any]:
        async with self._sessions() as session, session.begin():
            await self._project(session, claim.scope, write=False)
            source = await self._source(
                session, claim.scope, claim.source_id, write=False
            )
            run = await self._parse_run_optional(
                session, claim.scope, source, write=False
            )
            job = await self._job(
                session, claim.scope, claim.source_id, claim.job_id, write=False
            )
            batch = await self._batch(
                session,
                claim.scope,
                claim.source_id,
                claim.job_id,
                claim.batch_index,
                write=False,
            )
            if (
                batch.status != "running"
                or batch.attempt_token != claim.attempt_token
                or job.status != "running"
            ):
                raise AnalysisError("analysis_job_conflict", 409)
            if not self._lineage_matches(source, run, job):
                raise AnalysisError("analysis_job_stale", 409)
            if (
                job.provider != self._provider.provider
                or job.model != self._provider.model
                or job.prompt_version != PROMPT_VERSION
                or batch.provider != job.provider
                or batch.model != job.model
                or batch.prompt_version != job.prompt_version
            ):
                raise AnalysisError("analysis_job_unavailable", 503)
            section_ids = [UUID(item["section_id"]) for item in self._plan_slices(batch.plan)]
            rows = await self._sections_by_id(
                session, claim.scope, job.parse_run_id, section_ids
            )
            window = self._window(batch.plan, rows)

        try:
            raw = await self._provider.generate(window)
        except AnalysisError:
            raise
        except Exception:
            raise AnalysisError("analysis_unavailable", 502) from None
        return validate_candidate(raw, window)

    async def complete_batch(
        self, claim: BatchClaim, candidate: dict[str, Any]
    ) -> bool:
        async with self._sessions() as session, session.begin():
            await self._project(session, claim.scope, write=False)
            source = await self._source(session, claim.scope, claim.source_id, write=True)
            run = await self._parse_run_optional(
                session, claim.scope, source, write=True
            )
            job = await self._job(
                session, claim.scope, claim.source_id, claim.job_id, write=True
            )
            batch = await self._batch(
                session,
                claim.scope,
                claim.source_id,
                claim.job_id,
                claim.batch_index,
                write=True,
            )
            if job.status in {"succeeded", "stale"}:
                return False
            if not self._lineage_matches(source, run, job):
                job.status = "stale"
                job.error_code = "source_changed"
                job.updated_at = datetime.now(UTC)
                await session.flush()
                return False
            if batch.status != "running" or batch.attempt_token != claim.attempt_token:
                return False
            batch.result = deepcopy(candidate)
            batch.status = "succeeded"
            batch.error_code = None
            batch.updated_at = datetime.now(UTC)
            job.completed_batches += 1
            job.updated_at = batch.updated_at
            await session.flush()
            return True

    async def fail_batch(self, claim: BatchClaim, error_code: str) -> bool:
        sanitized = error_code if error_code in _BATCH_FAILURE_CODES else "analysis_unavailable"
        async with self._sessions() as session, session.begin():
            await self._project(session, claim.scope, write=False)
            source = await self._source(
                session, claim.scope, claim.source_id, write=True
            )
            run = await self._parse_run_optional(
                session, claim.scope, source, write=True
            )
            job = await self._job(
                session, claim.scope, claim.source_id, claim.job_id, write=True
            )
            batch = await self._batch(
                session,
                claim.scope,
                claim.source_id,
                claim.job_id,
                claim.batch_index,
                write=True,
            )
            if job.status in {"succeeded", "stale"}:
                return False
            if not self._lineage_matches(source, run, job):
                job.status = "stale"
                job.error_code = "source_changed"
                job.updated_at = datetime.now(UTC)
                await session.flush()
                return False
            if batch.status != "running" or batch.attempt_token != claim.attempt_token:
                return False
            now = datetime.now(UTC)
            batch.status = "failed"
            batch.error_code = sanitized
            batch.updated_at = now
            job.status = "failed"
            job.failed_batches += 1
            job.error_code = sanitized
            job.updated_at = now
            await session.flush()
            return True

    async def retry(
        self, scope: TenantProjectScope, source_id: UUID, job_id: UUID
    ) -> FullAnalysisJobStatus:
        stale = False
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=False)
            source = await self._source(session, scope, source_id, write=True)
            run = await self._parse_run_optional(session, scope, source, write=True)
            job = await self._job(session, scope, source_id, job_id, write=True)
            if job.status == "stale":
                raise AnalysisError("analysis_job_stale", 409)
            if job.status == "succeeded":
                raise AnalysisError("analysis_job_conflict", 409)
            if not self._lineage_matches(source, run, job):
                job.status = "stale"
                job.error_code = "source_changed"
                job.updated_at = datetime.now(UTC)
                stale = True
            elif job.status == "queued":
                if job.error_code != "workflow_dispatch_unavailable":
                    raise AnalysisError("analysis_job_conflict", 409)
            elif job.status == "failed":
                batches = await self._batches(session, scope, source_id, job_id, write=True)
                now = datetime.now(UTC)
                for batch in batches:
                    if batch.status == "failed":
                        batch.status = "pending"
                        batch.attempt_token = None
                        batch.error_code = None
                        batch.updated_at = now
                job.status = "queued"
                job.failed_batches = 0
            else:
                code = "analysis_job_stale" if job.status == "stale" else "analysis_job_conflict"
                raise AnalysisError(code, 409)
            if not stale:
                job.error_code = None
                job.updated_at = datetime.now(UTC)
            await session.flush()

        if stale:
            raise AnalysisError("analysis_job_stale", 409)
        await self._dispatch(job)
        return self._status(job)

    async def finalize(
        self, scope: TenantProjectScope, source_id: UUID, job_id: UUID
    ) -> UUID:
        """Publish one full result and link it to the job in one transaction."""
        stale = False
        analysis_id: UUID | None = None
        async with self._sessions() as session, session.begin():
            await self._project(session, scope, write=False)
            source = await self._source(session, scope, source_id, write=True)
            run = await self._parse_run_optional(session, scope, source, write=True)
            job = await self._job(session, scope, source_id, job_id, write=True)
            if job.final_analysis_id is not None:
                return job.final_analysis_id
            if job.status in {"stale", "succeeded"}:
                # Terminal jobs are immutable in the database (trigger guard).
                # A late/stale finalize must not attempt another UPDATE on them.
                code = "analysis_job_stale" if job.status == "stale" else "analysis_job_conflict"
                raise AnalysisError(code, 409)
            batches = await self._batches(
                session, scope, source_id, job_id, write=True
            )
            if not self._lineage_matches(source, run, job):
                job.status = "stale"
                job.error_code = "source_changed"
                job.updated_at = datetime.now(UTC)
                await session.flush()
                stale = True
            else:
                self._require_finalizable(job, batches)
                merged = merge_batches(
                    [
                        BatchCandidate(index=batch.batch_index, candidate=batch.result)
                        for batch in batches
                        if batch.result is not None
                    ]
                )
                characters = merged["characters"]
                if len(characters) > MAX_HISTORY_CHARACTERS:
                    raise AnalysisError("analysis_result_too_large", 422)
                character_ids = [str(uuid4()) for _ in characters]
                suggestions = self._suggestions_with_character_ids(
                    merged.get("merge_suggestions", []), character_ids
                )
                original = {
                    "source_document_id": str(source_id),
                    "parse_run_id": str(job.parse_run_id),
                    "product_version": job.product_version,
                    "product_sha256": job.product_sha256,
                    "provider": job.provider,
                    "model": job.model,
                    "prompt_version": job.prompt_version,
                    "coverage": {
                        "mode": "full",
                        "job_id": str(job.id),
                        "core_ranges": [
                            {
                                "batch_index": batch.batch_index,
                                "char_start": batch.core_start,
                                "char_end": batch.core_end,
                            }
                            for batch in batches
                        ],
                    },
                    "summary": merged["summary"],
                    "characters": characters,
                    "merge_suggestions": suggestions,
                }
                history = SourceAnalysisHistoryService(None, self._sessions)  # type: ignore[arg-type]
                appended = await history.append_validated_in_session(
                    session,
                    scope,
                    source_id,
                    job.created_by_subject,
                    original,
                    source=source,
                    run=run,
                    character_ids=character_ids,
                )
                analysis_id = UUID(appended["analysis_id"])
                job.final_analysis_id = analysis_id
                job.status = "succeeded"
                job.error_code = None
                job.updated_at = datetime.now(UTC)
                await session.flush()
        if stale:
            raise AnalysisError("analysis_job_stale", 409)
        assert analysis_id is not None
        return analysis_id

    @staticmethod
    def _require_finalizable(
        job: FullAnalysisJob, batches: Sequence[FullAnalysisBatch]
    ) -> None:
        indexes = [batch.batch_index for batch in batches]
        if (
            job.status != "running"
            or job.completed_batches != job.total_batches
            or job.failed_batches != 0
            or len(batches) != job.total_batches
            or indexes != list(range(job.total_batches))
            or any(batch.status != "succeeded" or batch.result is None for batch in batches)
        ):
            raise AnalysisError("analysis_job_incomplete", 409)

    @staticmethod
    def _suggestions_with_character_ids(
        suggestions: object, character_ids: Sequence[str]
    ) -> list[dict[str, Any]]:
        if not isinstance(suggestions, list):
            raise AnalysisError("analysis_job_unavailable", 503)
        mapped: list[dict[str, Any]] = []
        try:
            for suggestion in suggestions:
                if not isinstance(suggestion, dict):
                    raise TypeError
                indexes = suggestion.get("character_indexes")
                if not isinstance(indexes, list) or any(
                    type(index) is not int for index in indexes
                ):
                    raise TypeError
                item = deepcopy(suggestion)
                item.pop("character_indexes", None)
                item["character_ids"] = [character_ids[index] for index in indexes]
                mapped.append(item)
        except (IndexError, TypeError):
            raise AnalysisError("analysis_job_unavailable", 503) from None
        return mapped

    async def _dispatch(self, job: FullAnalysisJob) -> None:
        command = FullAnalysisJobInput(
            tenant_id=job.tenant_id,
            project_id=job.project_id,
            source_id=job.source_document_id,
            job_id=job.id,
        )
        workflow_id = full_analysis_workflow_id(job.id)
        try:
            await self._gateway.dispatch(command, workflow_id=workflow_id)
        except Exception:
            await self._record_dispatch_failure(job)

    async def _record_dispatch_failure(self, job: FullAnalysisJob) -> None:
        async with self._sessions() as session, session.begin():
            current = await self._job(
                session,
                TenantProjectScope(tenant_id=job.tenant_id, project_id=job.project_id),
                job.source_document_id,
                job.id,
                write=True,
            )
            if current.status == "queued":
                current.error_code = "workflow_dispatch_unavailable"
                current.updated_at = datetime.now(UTC)
                await session.flush()
            job.error_code = current.error_code
            job.status = current.status

    async def _project(
        self, session: AsyncSession, scope: TenantProjectScope, *, write: bool
    ) -> Project:
        result = await session.execute(
            select(Project)
            .where(
                Project.tenant_id == scope.tenant_id,
                Project.id == scope.project_id,
                Project.status == "active",
            )
            .with_for_update(read=not write)
        )
        project = result.scalar_one_or_none()
        if project is None:
            raise AnalysisError("source_not_found", 404)
        return project

    async def _source(
        self,
        session: AsyncSession,
        scope: TenantProjectScope,
        source_id: UUID,
        *,
        write: bool,
    ) -> SourceDocument:
        result = await session.execute(
            select(SourceDocument)
            .where(
                SourceDocument.tenant_id == scope.tenant_id,
                SourceDocument.project_id == scope.project_id,
                SourceDocument.id == source_id,
            )
            .with_for_update(read=not write)
        )
        source = result.scalar_one_or_none()
        if source is None:
            raise AnalysisError("source_not_found", 404)
        return source

    async def _parse_run(
        self,
        session: AsyncSession,
        scope: TenantProjectScope,
        source: SourceDocument,
        *,
        write: bool,
    ) -> SourceParseRun:
        run = await self._parse_run_optional(session, scope, source, write=write)
        if run is None:
            raise AnalysisError("source_not_ready", 409)
        return run

    async def _parse_run_optional(
        self,
        session: AsyncSession,
        scope: TenantProjectScope,
        source: SourceDocument,
        *,
        write: bool,
    ) -> SourceParseRun | None:
        result = await session.execute(
            select(SourceParseRun)
            .where(
                SourceParseRun.tenant_id == scope.tenant_id,
                SourceParseRun.project_id == scope.project_id,
                SourceParseRun.source_document_id == source.id,
                SourceParseRun.id == source.active_parse_run_id,
            )
            .with_for_update(read=not write)
        )
        run = result.scalar_one_or_none()
        return run

    async def _sections(
        self,
        session: AsyncSession,
        scope: TenantProjectScope,
        parse_run_id: UUID,
        *,
        write: bool,
    ) -> list[SourceSection]:
        result = await session.execute(
            select(SourceSection)
            .where(
                SourceSection.tenant_id == scope.tenant_id,
                SourceSection.project_id == scope.project_id,
                SourceSection.parse_run_id == parse_run_id,
            )
            .order_by(SourceSection.section_index)
            .with_for_update(read=not write)
        )
        return list(result.scalars().all())

    async def _sections_by_id(
        self,
        session: AsyncSession,
        scope: TenantProjectScope,
        parse_run_id: UUID,
        section_ids: Sequence[UUID],
    ) -> list[SourceSection]:
        result = await session.execute(
            select(SourceSection)
            .where(
                SourceSection.tenant_id == scope.tenant_id,
                SourceSection.project_id == scope.project_id,
                SourceSection.parse_run_id == parse_run_id,
                SourceSection.id.in_(section_ids),
            )
            .order_by(SourceSection.section_index)
            .with_for_update(read=True)
        )
        return list(result.scalars().all())

    async def _job(
        self,
        session: AsyncSession,
        scope: TenantProjectScope,
        source_id: UUID,
        job_id: UUID,
        *,
        write: bool,
    ) -> FullAnalysisJob:
        result = await session.execute(
            select(FullAnalysisJob)
            .where(
                FullAnalysisJob.tenant_id == scope.tenant_id,
                FullAnalysisJob.project_id == scope.project_id,
                FullAnalysisJob.source_document_id == source_id,
                FullAnalysisJob.id == job_id,
            )
            .with_for_update(read=not write)
        )
        job = result.scalar_one_or_none()
        if job is None:
            raise AnalysisError("analysis_job_not_found", 404)
        return job

    async def _batch(
        self,
        session: AsyncSession,
        scope: TenantProjectScope,
        source_id: UUID,
        job_id: UUID,
        batch_index: int,
        *,
        write: bool,
    ) -> FullAnalysisBatch:
        result = await session.execute(
            select(FullAnalysisBatch)
            .where(
                FullAnalysisBatch.tenant_id == scope.tenant_id,
                FullAnalysisBatch.project_id == scope.project_id,
                FullAnalysisBatch.source_document_id == source_id,
                FullAnalysisBatch.job_id == job_id,
                FullAnalysisBatch.batch_index == batch_index,
            )
            .with_for_update(read=not write)
        )
        batch = result.scalar_one_or_none()
        if batch is None:
            raise AnalysisError("analysis_batch_not_found", 404)
        return batch

    async def _batches(
        self,
        session: AsyncSession,
        scope: TenantProjectScope,
        source_id: UUID,
        job_id: UUID,
        *,
        write: bool,
    ) -> list[FullAnalysisBatch]:
        result = await session.execute(
            select(FullAnalysisBatch)
            .where(
                FullAnalysisBatch.tenant_id == scope.tenant_id,
                FullAnalysisBatch.project_id == scope.project_id,
                FullAnalysisBatch.source_document_id == source_id,
                FullAnalysisBatch.job_id == job_id,
            )
            .order_by(FullAnalysisBatch.batch_index)
            .with_for_update(read=not write)
        )
        return list(result.scalars().all())

    @staticmethod
    def _require_ready(source: SourceDocument, run: SourceParseRun) -> None:
        if (
            source.status != "chunks_ready"
            or run.status != "succeeded"
            or run.product_version != source.aggregate_version
            or not run.product_sha256
        ):
            raise AnalysisError("source_not_ready", 409)

    @staticmethod
    def _lineage_matches(
        source: SourceDocument, run: SourceParseRun | None, job: FullAnalysisJob
    ) -> bool:
        return (
            source.status == "chunks_ready"
            and source.active_parse_run_id == job.parse_run_id
            and source.aggregate_version == job.product_version
            and run is not None
            and run.id == job.parse_run_id
            and run.status == "succeeded"
            and run.product_version == job.product_version
            and run.product_sha256 == job.product_sha256
        )

    @staticmethod
    def _section(row: SourceSection) -> Section:
        return Section(
            id=row.id,
            index=row.section_index,
            heading=row.heading,
            text=row.text,
            char_start=row.char_start,
            original_length=len(row.text),
            source_ref_kind=row.source_ref_kind,
            source_ref_index=row.source_ref_index,
            source_ref_subindex=row.source_ref_subindex,
        )

    @staticmethod
    def _new_batch(
        scope: TenantProjectScope,
        source_id: UUID,
        job: FullAnalysisJob,
        plan: BatchPlan,
        now: datetime,
    ) -> FullAnalysisBatch:
        return FullAnalysisBatch(
            id=uuid4(),
            tenant_id=scope.tenant_id,
            project_id=scope.project_id,
            source_document_id=source_id,
            job_id=job.id,
            batch_index=plan.index,
            core_start=plan.core_start,
            core_end=plan.core_end,
            context_char_count=plan.context_char_count,
            plan={
                "slices": [
                    {
                        "section_id": str(item.section_id),
                        "section_index": item.section_index,
                        "char_start": item.char_start,
                        "char_end": item.char_start + len(item.text),
                        "core_start": item.core_start,
                        "core_end": item.core_end,
                        "context": item.context,
                    }
                    for item in plan.slices
                ]
            },
            status="pending",
            attempt_token=None,
            attempts=0,
            result=None,
            provider=job.provider,
            model=job.model,
            prompt_version=job.prompt_version,
            error_code=None,
            created_at=now,
            updated_at=now,
        )

    @staticmethod
    def _plan_slices(plan: dict[str, Any]) -> list[dict[str, Any]]:
        try:
            slices = plan.get("slices")
            if not isinstance(slices, list) or not slices:
                raise ValueError
            required_integers = (
                "section_index",
                "char_start",
                "char_end",
                "core_start",
                "core_end",
            )
            for item in slices:
                if not isinstance(item, dict):
                    raise TypeError
                UUID(item["section_id"])
                if any(type(item[field]) is not int for field in required_integers):
                    raise TypeError
                if type(item["context"]) is not bool:
                    raise TypeError
                if (
                    item["section_index"] < 0
                    or item["char_start"] < 0
                    or item["char_end"] <= item["char_start"]
                    or item["core_start"] < 0
                    or item["core_end"] < item["core_start"]
                ):
                    raise ValueError
        except (AttributeError, KeyError, TypeError, ValueError):
            raise AnalysisError("analysis_job_unavailable", 503) from None
        return cast(list[dict[str, Any]], slices)

    @classmethod
    def _window(
        cls, plan: dict[str, Any], rows: Sequence[SourceSection]
    ) -> Window:
        by_id = {str(row.id): row for row in rows}
        materialized: list[Section] = []
        for item in cls._plan_slices(plan):
            try:
                row = by_id[item["section_id"]]
                start = int(item["char_start"])
                end = int(item["char_end"])
                offset = start - row.char_start
                text = row.text[offset : offset + end - start]
                if offset < 0 or len(text) != end - start:
                    raise ValueError
            except (KeyError, TypeError, ValueError):
                raise AnalysisError("analysis_job_unavailable", 503) from None
            section = Section(
                id=row.id,
                index=row.section_index,
                heading=row.heading,
                text=text,
                char_start=start,
                original_length=len(text),
                source_ref_kind=row.source_ref_kind,
                source_ref_index=row.source_ref_index,
                source_ref_subindex=row.source_ref_subindex,
            )
            if (
                materialized
                and materialized[-1].id == section.id
                and materialized[-1].char_start + len(materialized[-1].text) == section.char_start
            ):
                previous = materialized.pop()
                materialized.append(
                    Section(
                        id=previous.id,
                        index=previous.index,
                        heading=previous.heading,
                        text=previous.text + section.text,
                        char_start=previous.char_start,
                        original_length=len(previous.text) + len(section.text),
                        source_ref_kind=previous.source_ref_kind,
                        source_ref_index=previous.source_ref_index,
                        source_ref_subindex=previous.source_ref_subindex,
                    )
                )
            else:
                materialized.append(section)
        return Window(
            sections=tuple(materialized),
            char_count=sum(len(item.text) for item in materialized),
            partial=True,
        )

    @staticmethod
    def _status(job: FullAnalysisJob) -> FullAnalysisJobStatus:
        return FullAnalysisJobStatus(
            job_id=job.id,
            status=job.status,
            total_batches=job.total_batches,
            completed_batches=job.completed_batches,
            failed_batches=job.failed_batches,
            analysis_id=job.final_analysis_id,
            error_code=job.error_code,
            dispatch_pending=(
                job.status == "queued" and job.error_code == "workflow_dispatch_unavailable"
            ),
        )


__all__ = [
    "BatchClaim",
    "FullAnalysisJobInput",
    "FullAnalysisJobService",
    "FullAnalysisJobStatus",
    "FullAnalysisWorkflowGateway",
    "full_analysis_workflow_id",
]
