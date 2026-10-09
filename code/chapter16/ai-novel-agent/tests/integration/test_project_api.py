from __future__ import annotations

import logging
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from novel_agent.api.dependencies import get_principal
from novel_agent.api.routes.projects import (
    ProjectConflictError,
    get_project_service,
)
from novel_agent.auth.models import Principal
from novel_agent.config import Settings
from novel_agent.db.models.project import Project, StoryBranch
from novel_agent.main import create_app
from novel_agent.projects.schemas import CreatedProject, CreateProject

TENANT_A = uuid.UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
TENANT_B = uuid.UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
USER_SUBJECT = "oidc|user-1"
PROJECT_ID = uuid.UUID("dddddddd-dddd-4ddd-8ddd-dddddddddddd")
BRANCH_ID = uuid.UUID("eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee")
SENSITIVE_EXCEPTION_DETAIL = "database " + "password=do-not-leak"


def _created_project(*, tenant_id: uuid.UUID = TENANT_A) -> CreatedProject:
    project = Project(
        id=PROJECT_ID,
        tenant_id=tenant_id,
        creation_request_id="api-test-1",
        slug="long-night",
        title="长夜",
        language="zh-CN",
        status="active",
    )
    branch = StoryBranch(
        id=BRANCH_ID,
        tenant_id=tenant_id,
        project_id=PROJECT_ID,
        name="main",
        is_default=True,
    )
    return CreatedProject(project=project, default_branch=branch)


class FakeProjectService:
    def __init__(self, projects: list[CreatedProject] | None = None) -> None:
        self.projects = projects or []
        self.tenant_ids: list[uuid.UUID] = []
        self.actor_ids: list[str] = []

    async def create(
        self, tenant_id: uuid.UUID, actor_id: str, request: CreateProject
    ) -> CreatedProject:
        del request
        self.tenant_ids.append(tenant_id)
        self.actor_ids.append(actor_id)
        result = _created_project(tenant_id=tenant_id)
        self.projects.append(result)
        return result

    async def list(self, tenant_id: uuid.UUID) -> list[CreatedProject]:
        self.tenant_ids.append(tenant_id)
        return [item for item in self.projects if item.project.tenant_id == tenant_id]

    async def get(
        self, tenant_id: uuid.UUID, project_id: uuid.UUID
    ) -> CreatedProject | None:
        self.tenant_ids.append(tenant_id)
        return next(
            (
                item
                for item in self.projects
                if item.project.tenant_id == tenant_id and item.project.id == project_id
            ),
            None,
        )


class ConflictProjectService(FakeProjectService):
    async def create(
        self, tenant_id: uuid.UUID, actor_id: str, request: CreateProject
    ) -> CreatedProject:
        del tenant_id, actor_id, request
        raise ProjectConflictError


class IntegrityFailureProjectService(FakeProjectService):
    async def create(
        self, tenant_id: uuid.UUID, actor_id: str, request: CreateProject
    ) -> CreatedProject:
        del tenant_id, actor_id, request
        raise IntegrityError("INSERT", {}, RuntimeError("unique constraint"))


class IntegrityReadProjectService(FakeProjectService):
    async def list(self, tenant_id: uuid.UUID) -> list[CreatedProject]:
        del tenant_id
        raise IntegrityError("SELECT", {}, RuntimeError("database corruption"))


async def _raise_sensitive_database_error() -> None:
    raise RuntimeError(SENSITIVE_EXCEPTION_DETAIL)


class BrokenProjectService(FakeProjectService):
    async def list(self, tenant_id: uuid.UUID) -> list[CreatedProject]:
        del tenant_id
        await _raise_sensitive_database_error()
        raise AssertionError("unreachable")


def _client(
    service: FakeProjectService,
    *,
    principal: Principal | None = None,
    raise_server_exceptions: bool = True,
) -> TestClient:
    app = create_app(Settings(environment="test"))
    app.dependency_overrides[get_project_service] = lambda: service
    if principal is not None:
        app.dependency_overrides[get_principal] = lambda: principal
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _principal(tenant_id: uuid.UUID = TENANT_A) -> Principal:
    return Principal(subject=USER_SUBJECT, tenant_id=tenant_id)


def test_create_project_uses_principal_tenant() -> None:
    service = FakeProjectService()
    with _client(service, principal=_principal()) as client:
        response = client.post(
            "/api/v1/projects",
            json={"request_id": "api-test-1", "title": "长夜", "language": "zh-CN"},
        )

    assert response.status_code == 201
    assert response.json()["title"] == "长夜"
    assert response.json()["default_branch"]["name"] == "main"
    assert service.tenant_ids == [TENANT_A]
    assert service.actor_ids == [USER_SUBJECT]


def test_list_projects_is_scoped_to_principal_tenant() -> None:
    service = FakeProjectService([_created_project(tenant_id=TENANT_A), _created_project(tenant_id=TENANT_B)])
    with _client(service, principal=_principal()) as client:
        response = client.get("/api/v1/projects")

    assert response.status_code == 200
    assert [item["id"] for item in response.json()] == [str(PROJECT_ID)]
    assert service.tenant_ids == [TENANT_A]


def test_get_project_is_scoped_to_principal_tenant() -> None:
    service = FakeProjectService([_created_project(tenant_id=TENANT_A)])
    with _client(service, principal=_principal()) as client:
        response = client.get(f"/api/v1/projects/{PROJECT_ID}")

    assert response.status_code == 200
    assert response.json()["id"] == str(PROJECT_ID)
    assert service.tenant_ids == [TENANT_A]


def test_get_project_from_another_tenant_returns_404() -> None:
    service = FakeProjectService([_created_project(tenant_id=TENANT_B)])
    with _client(service, principal=_principal()) as client:
        response = client.get(f"/api/v1/projects/{PROJECT_ID}")

    assert response.status_code == 404
    assert response.json()["status"] == 404


def test_unknown_json_fields_are_rejected() -> None:
    with _client(FakeProjectService(), principal=_principal()) as client:
        response = client.post(
            "/api/v1/projects",
            json={
                "request_id": "api-test-1",
                "title": "长夜",
                "language": "zh-CN",
                "tenant_id": str(TENANT_B),
            },
        )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")


def test_invalid_chinese_title_returns_problem_details() -> None:
    with _client(FakeProjectService(), principal=_principal()) as client:
        response = client.post(
            "/api/v1/projects",
            json={"request_id": "api-test-1", "title": "　 ", "language": "zh-CN"},
        )

    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/problem+json")
    assert response.json()["type"] == "https://novel-agent.dev/problems/validation-error"
    assert response.json()["title"] == "Request validation failed"
    assert response.json()["status"] == 422
    assert response.json()["trace_id"]


def test_duplicate_project_conflict_is_safe() -> None:
    with _client(ConflictProjectService(), principal=_principal()) as client:
        response = client.post(
            "/api/v1/projects",
            json={"request_id": "api-test-1", "title": "长夜", "language": "zh-CN"},
        )

    assert response.status_code == 409
    assert response.json()["status"] == 409


def test_raw_integrity_error_during_create_is_not_misreported_as_conflict() -> None:
    with _client(
        IntegrityFailureProjectService(),
        principal=_principal(),
        raise_server_exceptions=False,
    ) as client:
        response = client.post(
            "/api/v1/projects",
            json={"request_id": "api-test-1", "title": "长夜", "language": "zh-CN"},
        )

    assert response.status_code == 500
    assert response.json()["status"] == 500


def test_unrelated_integrity_error_is_not_misreported_as_project_conflict() -> None:
    with _client(
        IntegrityReadProjectService(),
        principal=_principal(),
        raise_server_exceptions=False,
    ) as client:
        response = client.get("/api/v1/projects")

    assert response.status_code == 500
    assert response.json()["status"] == 500


def test_project_routes_require_authentication() -> None:
    with _client(FakeProjectService()) as client:
        response = client.get("/api/v1/projects")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"


def test_unexpected_error_is_logged_with_response_trace_id_without_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.ERROR, logger="novel_agent.api.errors")
    with _client(
        BrokenProjectService(),
        principal=_principal(),
        raise_server_exceptions=False,
    ) as client:
        response = client.get(
            "/api/v1/projects",
            headers={"X-Request-ID": "header-secret", "Authorization": "Bearer token-secret"},
        )

    assert response.status_code == 500
    assert response.headers["content-type"].startswith("application/problem+json")
    trace_id = response.json()["trace_id"]
    assert trace_id
    assert response.headers["x-trace-id"] == trace_id
    assert trace_id != "header-secret"
    assert any(getattr(record, "trace_id", None) == trace_id for record in caplog.records)
    assert trace_id in caplog.text
    assert "exception_type=RuntimeError" in caplog.text
    assert "_raise_sensitive_database_error" in caplog.text
    assert "password" not in response.text
    assert "RuntimeError" not in response.text
    assert SENSITIVE_EXCEPTION_DETAIL not in caplog.text
    assert "header-secret" not in caplog.text
    assert "token-secret" not in caplog.text
