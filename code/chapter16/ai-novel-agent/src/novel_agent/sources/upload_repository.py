from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import cast

from sqlalchemy import and_, func, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from novel_agent.db.models.project import Project
from novel_agent.db.models.source import SourceDocument, SourceUploadSession
from novel_agent.sources.contracts import InitiateSourceUpload
from novel_agent.sources.errors import (
    SourceConflictError,
    UploadQuotaExceededError,
)


@dataclass(frozen=True)
class UploadClaim:
    session: SourceUploadSession
    attempt_token: uuid.UUID | None
    already_consumed: bool = False
    in_progress: bool = False


def _declaration_matches(
    row: SourceUploadSession, command: InitiateSourceUpload, actor_id: str
) -> bool:
    return (
        row.original_display_name == command.filename
        and row.file_type == command.file_type.value
        and row.declared_media_type == command.media_type
        and row.byte_count == command.byte_size
        and row.content_sha256 == command.content_sha256
        and row.authorization_declaration == command.authorization.model_dump(mode="json")
        and row.declared_by_subject == actor_id
    )


class SqlAlchemyUploadRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def project_exists(self, tenant_id: uuid.UUID, project_id: uuid.UUID) -> bool:
        return (
            await self._session.scalar(
                select(Project.id).where(
                    Project.tenant_id == tenant_id,
                    Project.id == project_id,
                    Project.status == "active",
                )
            )
        ) is not None

    async def create_or_get(
        self,
        *,
        tenant_id: uuid.UUID,
        project_id: uuid.UUID,
        source_id: uuid.UUID,
        upload_id: uuid.UUID,
        command: InitiateSourceUpload,
        actor_id: str,
        expires_at: datetime,
        max_pending: int,
    ) -> SourceUploadSession:
        existing = cast(
            SourceUploadSession | None,
            await self._session.scalar(
                select(SourceUploadSession).where(
                    SourceUploadSession.tenant_id == tenant_id,
                    SourceUploadSession.project_id == project_id,
                    SourceUploadSession.request_id == command.request_id,
                )
            ),
        )
        if existing is not None:
            if not _declaration_matches(existing, command, actor_id):
                raise SourceConflictError("upload request metadata conflicts")
            if existing.status == "expired" or existing.expires_at <= datetime.now(
                expires_at.tzinfo
            ):
                existing.status = "expired"
                existing.attempt_token = None
                existing.lease_expires_at = None
                return existing
            if existing.status != "pending":
                raise SourceConflictError("upload session cannot issue another signed URL")
            return existing
        # Serialize reservations per project so the bounded pending quota cannot race.
        await self._session.scalar(
            select(Project.id)
            .where(Project.tenant_id == tenant_id, Project.id == project_id)
            .with_for_update()
        )
        active_count = await self._session.scalar(
            select(func.count())
            .select_from(SourceUploadSession)
            .where(
                SourceUploadSession.tenant_id == tenant_id,
                SourceUploadSession.project_id == project_id,
                SourceUploadSession.status.in_(("pending", "validating")),
                SourceUploadSession.expires_at > datetime.now(expires_at.tzinfo),
            )
        )
        if (active_count or 0) >= max_pending:
            raise UploadQuotaExceededError
        values = {
            "upload_id": upload_id,
            "source_document_id": source_id,
            "tenant_id": tenant_id,
            "project_id": project_id,
            "request_id": command.request_id,
            "original_display_name": command.filename,
            "file_type": command.file_type.value,
            "declared_media_type": command.media_type,
            "byte_count": command.byte_size,
            "content_sha256": command.content_sha256,
            "authorization_declaration": command.authorization.model_dump(mode="json"),
            "declared_by_subject": actor_id,
            "status": "pending",
            "expires_at": expires_at,
        }
        row = cast(
            SourceUploadSession | None,
            await self._session.scalar(
                insert(SourceUploadSession)
                .values(**values)
                .on_conflict_do_nothing(constraint="uq_source_upload_scope_request")
                .returning(SourceUploadSession)
            ),
        )
        if row is not None:
            return row
        row = cast(
            SourceUploadSession | None,
            await self._session.scalar(
                select(SourceUploadSession).where(
                    SourceUploadSession.tenant_id == tenant_id,
                    SourceUploadSession.project_id == project_id,
                    SourceUploadSession.request_id == command.request_id,
                )
            ),
        )
        if row is None or not _declaration_matches(row, command, actor_id):
            raise SourceConflictError("upload request metadata conflicts")
        return row

    async def claim(
        self,
        *,
        tenant_id: uuid.UUID,
        project_id: uuid.UUID,
        upload_id: uuid.UUID,
        completion_request_id: str,
        actor_id: str,
        expected_byte_count: int,
        expected_sha256: str,
        now: datetime,
        lease_expires_at: datetime,
    ) -> UploadClaim | None:
        row = cast(
            SourceUploadSession | None,
            await self._session.scalar(
                select(SourceUploadSession)
                .where(
                    SourceUploadSession.tenant_id == tenant_id,
                    SourceUploadSession.project_id == project_id,
                    SourceUploadSession.upload_id == upload_id,
                )
                .with_for_update()
            ),
        )
        if row is None:
            return None
        if row.declared_by_subject != actor_id:
            return None
        if row.byte_count != expected_byte_count or row.content_sha256 != expected_sha256:
            raise SourceConflictError("completion metadata conflicts with upload declaration")
        if row.status == "consumed":
            if row.completion_request_id != completion_request_id:
                raise SourceConflictError("upload was consumed by a different completion")
            return UploadClaim(row, None, already_consumed=True)
        if row.expires_at <= now:
            row.status = "expired"
            row.attempt_token = None
            row.lease_expires_at = None
            return UploadClaim(row, None)
        if row.status == "validating" and row.lease_expires_at is not None:
            if row.lease_expires_at > now:
                if row.completion_request_id != completion_request_id:
                    raise SourceConflictError("upload validation is already in progress")
                return UploadClaim(row, None, in_progress=True)
        attempt = uuid.uuid4()
        row.status = "validating"
        row.completion_request_id = completion_request_id
        row.attempt_token = attempt
        row.lease_expires_at = lease_expires_at
        return UploadClaim(row, attempt)

    async def release(
        self,
        *,
        tenant_id: uuid.UUID,
        project_id: uuid.UUID,
        upload_id: uuid.UUID,
        attempt_token: uuid.UUID,
    ) -> bool:
        row = cast(
            SourceUploadSession | None,
            await self._session.scalar(
                select(SourceUploadSession)
                .where(
                    SourceUploadSession.tenant_id == tenant_id,
                    SourceUploadSession.project_id == project_id,
                    SourceUploadSession.upload_id == upload_id,
                )
                .with_for_update()
            ),
        )
        if row is None or row.status != "validating" or row.attempt_token != attempt_token:
            return False
        row.status = "pending"
        row.completion_request_id = None
        row.attempt_token = None
        row.lease_expires_at = None
        return True

    async def finalize(
        self,
        *,
        tenant_id: uuid.UUID,
        project_id: uuid.UUID,
        upload_id: uuid.UUID,
        attempt_token: uuid.UUID,
        consumed_at: datetime,
    ) -> SourceUploadSession | None:
        row = cast(
            SourceUploadSession | None,
            await self._session.scalar(
                select(SourceUploadSession)
                .where(
                    SourceUploadSession.tenant_id == tenant_id,
                    SourceUploadSession.project_id == project_id,
                    SourceUploadSession.upload_id == upload_id,
                )
                .with_for_update()
            ),
        )
        if row is None or row.status != "validating" or row.attempt_token != attempt_token:
            return None
        row.status = "consumed"
        row.consumed_at = consumed_at
        row.lease_expires_at = None
        row.attempt_token = None
        return row

    async def get_source(
        self, tenant_id: uuid.UUID, project_id: uuid.UUID, source_id: uuid.UUID
    ) -> SourceDocument | None:
        return cast(
            SourceDocument | None,
            await self._session.scalar(
                select(SourceDocument).where(
                    SourceDocument.tenant_id == tenant_id,
                    SourceDocument.project_id == project_id,
                    SourceDocument.id == source_id,
                )
            ),
        )

    async def list_sources_page(
        self,
        tenant_id: uuid.UUID,
        project_id: uuid.UUID,
        *,
        limit: int,
        cursor: tuple[datetime, uuid.UUID] | None,
    ) -> list[SourceDocument]:
        statement = select(SourceDocument).where(
            SourceDocument.tenant_id == tenant_id,
            SourceDocument.project_id == project_id,
        )
        if cursor is not None:
            created_at, source_id = cursor
            statement = statement.where(
                or_(
                    SourceDocument.created_at < created_at,
                    and_(
                        SourceDocument.created_at == created_at,
                        SourceDocument.id < source_id,
                    ),
                )
            )
        return list(
            await self._session.scalars(
                statement.order_by(
                    SourceDocument.created_at.desc(), SourceDocument.id.desc()
                ).limit(limit + 1)
            )
        )


__all__ = ["SqlAlchemyUploadRepository", "UploadClaim"]
