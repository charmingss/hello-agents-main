from uuid import UUID

from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novel_agent.analysis.contracts import (
    MAX_CHARS,
    MAX_SECTIONS,
    AnalysisError,
    Section,
    SourceIdentity,
    Window,
)
from novel_agent.analysis.window import build_window
from novel_agent.db.models.project import Project
from novel_agent.db.models.source import SourceDocument, SourceParseRun, SourceSection
from novel_agent.vector.schema import TenantProjectScope


class SqlAlchemyAnalysisAuthority:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._sessions = session_factory

    async def _identity(
        self, session: AsyncSession, scope: TenantProjectScope, source_id: UUID
    ) -> SourceIdentity:
        row = (
            await session.execute(
                select(SourceDocument, SourceParseRun)
                .join(
                    Project,
                    and_(
                        Project.id == SourceDocument.project_id,
                        Project.tenant_id == SourceDocument.tenant_id,
                    ),
                )
                .outerjoin(
                    SourceParseRun,
                    and_(
                        SourceParseRun.id == SourceDocument.active_parse_run_id,
                        SourceParseRun.source_document_id == SourceDocument.id,
                        SourceParseRun.tenant_id == SourceDocument.tenant_id,
                        SourceParseRun.project_id == SourceDocument.project_id,
                    ),
                )
                .where(
                    Project.tenant_id == scope.tenant_id,
                    Project.id == scope.project_id,
                    Project.status == "active",
                    SourceDocument.id == source_id,
                )
            )
        ).one_or_none()
        if row is None:
            raise AnalysisError("source_not_found", 404)
        source, run = row
        if (
            source.status != "chunks_ready"
            or run is None
            or run.status != "succeeded"
            or run.product_version != source.aggregate_version
            or not run.product_sha256
        ):
            raise AnalysisError("source_not_ready", 409)
        return SourceIdentity(
            source.id, run.id, run.product_version, run.product_sha256, source.original_display_name
        )

    async def load(
        self, scope: TenantProjectScope, source_id: UUID
    ) -> tuple[SourceIdentity, Window]:
        async with self._sessions() as session:
            identity = await self._identity(session, scope, source_id)
            rows = (
                await session.execute(
                    select(
                        SourceSection.id,
                        SourceSection.section_index,
                        SourceSection.heading,
                        func.substr(SourceSection.text, 1, MAX_CHARS + 1),
                        SourceSection.char_start,
                        func.length(SourceSection.text),
                        SourceSection.source_ref_kind,
                        SourceSection.source_ref_index,
                        SourceSection.source_ref_subindex,
                    )
                    .where(
                        SourceSection.tenant_id == scope.tenant_id,
                        SourceSection.project_id == scope.project_id,
                        SourceSection.parse_run_id == identity.parse_run_id,
                    )
                    .order_by(SourceSection.section_index)
                    .limit(MAX_SECTIONS + 1)
                )
            ).all()
            sections = [Section(*row) for row in rows]
        return identity, build_window(sections)

    async def recheck(self, scope: TenantProjectScope, identity: SourceIdentity) -> None:
        try:
            async with self._sessions() as session:
                current = await self._identity(session, scope, identity.source_id)
        except AnalysisError as exc:
            if exc.status == 404:
                raise
            raise AnalysisError("source_changed", 409) from None
        if current != identity:
            raise AnalysisError("source_changed", 409)
