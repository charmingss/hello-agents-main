import hashlib
import re
import unicodedata
import uuid
from typing import cast

from sqlalchemy import Select, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import ReturningInsert

from novel_agent.db.models.project import Project, StoryBranch
from novel_agent.projects.errors import ProjectConflictError
from novel_agent.projects.schemas import CreatedProject, CreateProject

_PROJECT_SLUG_CONSTRAINT = "uq_projects_tenant_slug"


def _slugify(value: str, request_id: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip().lower()
    base = "-".join(re.findall(r"[^\W_]+", normalized, flags=re.UNICODE)) or "project"
    suffix = hashlib.sha256(request_id.encode()).hexdigest()[:12]
    return f"{base[:87].rstrip('-') or 'project'}-{suffix}"


def _project_insert(
    tenant_id: uuid.UUID, request: CreateProject
) -> ReturningInsert[tuple[Project]]:
    return (
        insert(Project)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            creation_request_id=request.request_id,
            slug=_slugify(request.title, request.request_id),
            title=request.title,
            language=request.language,
        )
        .on_conflict_do_nothing(constraint="uq_projects_tenant_creation_request")
        .returning(Project)
    )


def _default_branch_insert(
    tenant_id: uuid.UUID, project_id: uuid.UUID
) -> ReturningInsert[tuple[StoryBranch]]:
    return (
        insert(StoryBranch)
        .values(
            id=uuid.uuid4(),
            tenant_id=tenant_id,
            project_id=project_id,
            name="main",
            is_default=True,
        )
        .on_conflict_do_update(
            constraint="uq_story_branches_tenant_project_name", set_={"is_default": True}
        )
        .returning(StoryBranch)
    )


def _constraint_name(error: IntegrityError) -> str | None:
    current: BaseException | None = error.orig
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        constraint_name = getattr(current, "constraint_name", None)
        if isinstance(constraint_name, str):
            return constraint_name
        diag = getattr(current, "diag", None)
        constraint_name = getattr(diag, "constraint_name", None)
        if isinstance(constraint_name, str):
            return constraint_name
        current = current.__cause__ or current.__context__
    return None


def _project_with_default_branch_select(
    tenant_id: uuid.UUID, project_id: uuid.UUID | None = None
) -> Select[tuple[Project, StoryBranch]]:
    statement = select(Project, StoryBranch).join(
        StoryBranch,
        (StoryBranch.tenant_id == Project.tenant_id)
        & (StoryBranch.project_id == Project.id)
        & StoryBranch.is_default.is_(True),
    )
    statement = statement.where(Project.tenant_id == tenant_id)
    if project_id is not None:
        statement = statement.where(Project.id == project_id)
    return statement


class SqlAlchemyProjectRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def create(self, tenant_id: uuid.UUID, request: CreateProject) -> Project:
        try:
            created = cast(
                Project | None, await self._session.scalar(_project_insert(tenant_id, request))
            )
        except IntegrityError as exc:
            if _constraint_name(exc) == _PROJECT_SLUG_CONSTRAINT:
                raise ProjectConflictError from exc
            raise
        if created is not None:
            return created

        winner = cast(
            Project | None,
            await self._session.scalar(
                select(Project).where(
                    Project.tenant_id == tenant_id,
                    Project.creation_request_id == request.request_id,
                )
            ),
        )
        if winner is None:
            raise RuntimeError("project request conflict resolved without a visible winning row")
        return winner

    async def get(self, tenant_id: uuid.UUID, project_id: uuid.UUID) -> Project | None:
        return cast(
            Project | None,
            await self._session.scalar(
                select(Project).where(Project.tenant_id == tenant_id, Project.id == project_id)
            ),
        )

    async def get_by_request_id(
        self, tenant_id: uuid.UUID, request_id: str
    ) -> CreatedProject | None:
        row = (
            await self._session.execute(
                _project_with_default_branch_select(tenant_id).where(
                    Project.creation_request_id == request_id
                )
            )
        ).one_or_none()
        if row is None:
            return None
        return CreatedProject(project=row.Project, default_branch=row.StoryBranch)

    async def create_default_branch(
        self, tenant_id: uuid.UUID, project_id: uuid.UUID
    ) -> StoryBranch:
        branch = cast(
            StoryBranch | None,
            await self._session.scalar(_default_branch_insert(tenant_id, project_id)),
        )
        if branch is None:
            raise RuntimeError("default branch upsert returned no row")
        return branch

    async def get_with_default_branch(
        self, tenant_id: uuid.UUID, project_id: uuid.UUID
    ) -> CreatedProject | None:
        row = (
            await self._session.execute(_project_with_default_branch_select(tenant_id, project_id))
        ).one_or_none()
        if row is None:
            return None
        return CreatedProject(project=row.Project, default_branch=row.StoryBranch)

    async def list_with_default_branch(self, tenant_id: uuid.UUID) -> list[CreatedProject]:
        rows = (
            await self._session.execute(
                _project_with_default_branch_select(tenant_id).order_by(Project.title, Project.id)
            )
        ).all()
        return [CreatedProject(project=row.Project, default_branch=row.StoryBranch) for row in rows]
