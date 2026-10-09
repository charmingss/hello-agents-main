import uuid
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, Request, status
from pydantic import BaseModel, ConfigDict

from novel_agent.api.dependencies import get_principal
from novel_agent.api.errors import ProjectNotFoundError
from novel_agent.auth.models import Principal
from novel_agent.outbox.repository import SqlAlchemyOutboxRepository
from novel_agent.projects.errors import ProjectConflictError
from novel_agent.projects.repository import SqlAlchemyProjectRepository
from novel_agent.projects.schemas import CreatedProject, CreateProject
from novel_agent.projects.service import ProjectService

router = APIRouter(prefix="/projects", tags=["projects"])


class BranchResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    name: str
    is_default: bool


class ProjectResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    id: uuid.UUID
    slug: str
    title: str
    language: str
    status: str
    default_branch: BranchResponse

    @classmethod
    def from_created(cls, created: CreatedProject) -> "ProjectResponse":
        return cls(
            id=created.project.id,
            slug=created.project.slug,
            title=created.project.title,
            language=created.project.language,
            status=created.project.status,
            default_branch=BranchResponse(
                id=created.default_branch.id,
                name=created.default_branch.name,
                is_default=created.default_branch.is_default,
            ),
        )


async def get_project_service(request: Request) -> AsyncIterator[ProjectService]:
    factory = request.app.state.session_factory
    async with factory() as session, session.begin():
        yield ProjectService(
            projects=SqlAlchemyProjectRepository(session),
            outbox=SqlAlchemyOutboxRepository(session),
        )


PrincipalDependency = Annotated[Principal, Depends(get_principal)]
ProjectServiceDependency = Annotated[ProjectService, Depends(get_project_service)]


@router.post("", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED)
async def create_project(
    command: CreateProject,
    principal: PrincipalDependency,
    service: ProjectServiceDependency,
) -> ProjectResponse:
    created = await service.create(principal.tenant_id, principal.subject, command)
    return ProjectResponse.from_created(created)


@router.get("", response_model=list[ProjectResponse])
async def list_projects(
    principal: PrincipalDependency,
    service: ProjectServiceDependency,
) -> list[ProjectResponse]:
    projects = await service.list(principal.tenant_id)
    return [ProjectResponse.from_created(project) for project in projects]


@router.get("/{project_id}", response_model=ProjectResponse)
async def get_project(
    project_id: uuid.UUID,
    principal: PrincipalDependency,
    service: ProjectServiceDependency,
) -> ProjectResponse:
    project = await service.get(principal.tenant_id, project_id)
    if project is None:
        raise ProjectNotFoundError
    return ProjectResponse.from_created(project)


__all__ = ["ProjectConflictError", "get_project_service", "router"]
