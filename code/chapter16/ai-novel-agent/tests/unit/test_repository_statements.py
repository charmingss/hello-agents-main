import json
import uuid

import pytest
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import IntegrityError

from novel_agent.outbox.events import ProjectCreatedV1
from novel_agent.outbox.repository import _outbox_insert, _outbox_values
from novel_agent.projects.errors import ProjectConflictError
from novel_agent.projects.repository import (
    SqlAlchemyProjectRepository,
    _default_branch_insert,
    _project_with_default_branch_select,
)
from novel_agent.projects.schemas import CreateProject


def _sql(statement: object) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


def test_default_branch_insert_restores_default_flag_on_name_conflict() -> None:
    statement = _default_branch_insert(uuid.uuid4(), uuid.uuid4())

    sql = _sql(statement)

    assert "ON CONFLICT ON CONSTRAINT uq_story_branches_tenant_project_name DO UPDATE" in sql
    assert "SET is_default =" in sql
    assert "RETURNING story_branches" in sql
    assert statement.compile().params["name"] == "main"
    assert statement.compile().params["is_default"] is True


def test_get_project_statement_scopes_project_and_branch_to_tenant() -> None:
    tenant_id = uuid.uuid4()
    project_id = uuid.uuid4()
    statement = _project_with_default_branch_select(tenant_id, project_id)

    sql = _sql(statement)
    params = statement.compile().params

    assert "story_branches.tenant_id = projects.tenant_id" in sql
    assert "projects.tenant_id =" in sql
    assert "projects.id =" in sql
    assert tenant_id in params.values()
    assert project_id in params.values()


def test_list_projects_statement_scopes_project_and_branch_to_tenant() -> None:
    tenant_id = uuid.uuid4()
    statement = _project_with_default_branch_select(tenant_id)

    sql = _sql(statement)
    params = statement.compile().params

    assert "story_branches.tenant_id = projects.tenant_id" in sql
    assert "projects.tenant_id =" in sql
    assert "projects.id =" not in sql.split("WHERE", maxsplit=1)[1]
    assert tenant_id in params.values()


class _DriverConstraintError(Exception):
    def __init__(self, constraint_name: str) -> None:
        self.constraint_name = constraint_name
        super().__init__("driver details")


def _integrity_error(constraint_name: str) -> IntegrityError:
    adapter_error = RuntimeError("adapter details")
    adapter_error.__cause__ = _DriverConstraintError(constraint_name)
    return IntegrityError("INSERT", {}, adapter_error)


class _FailingScalarSession:
    def __init__(self, error: IntegrityError) -> None:
        self.error = error

    async def scalar(self, statement: object) -> None:
        del statement
        raise self.error


@pytest.mark.asyncio
async def test_repository_translates_only_duplicate_slug_constraint() -> None:
    error = _integrity_error("uq_projects_tenant_slug")
    repository = SqlAlchemyProjectRepository(_FailingScalarSession(error))  # type: ignore[arg-type]

    with pytest.raises(ProjectConflictError):
        await repository.create(
            uuid.uuid4(),
            CreateProject(request_id="request-1", title="长夜", language="zh-CN"),
        )


@pytest.mark.asyncio
async def test_repository_preserves_non_slug_integrity_error() -> None:
    error = _integrity_error("fk_projects_tenant")
    repository = SqlAlchemyProjectRepository(_FailingScalarSession(error))  # type: ignore[arg-type]

    with pytest.raises(IntegrityError) as raised:
        await repository.create(
            uuid.uuid4(),
            CreateProject(request_id="request-1", title="长夜", language="zh-CN"),
        )

    assert raised.value is error


def test_outbox_insert_maps_complete_json_payload_and_unique_constraint() -> None:
    event = ProjectCreatedV1(
        event_id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        branch_id=uuid.uuid4(),
        actor_id="oidc|user-1",
        request_id="req-1",
        title="长夜",
        language="zh-CN",
        version=1,
    )

    values = _outbox_values(event)
    sql = _sql(_outbox_insert(event))

    assert values["id"] == event.event_id
    assert values["aggregate_type"] == "project"
    assert values["aggregate_id"] == event.project_id
    assert values["aggregate_version"] == 1
    assert values["event_type"] == "project.created.v1"
    assert values["payload"] == event.model_dump(mode="json")
    assert ProjectCreatedV1.model_validate_json(json.dumps(values["payload"])) == event
    assert values["payload"] == {
        "event_type": "project.created.v1",
        "event_id": str(event.event_id),
        "tenant_id": str(event.tenant_id),
        "project_id": str(event.project_id),
        "branch_id": str(event.branch_id),
        "actor_id": str(event.actor_id),
        "request_id": "req-1",
        "title": "长夜",
        "language": "zh-CN",
        "version": 1,
    }
    assert "ON CONFLICT DO NOTHING" in sql
    assert "RETURNING outbox_events" in sql
