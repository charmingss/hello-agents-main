import uuid

from alembic.config import Config
from sqlalchemy import ForeignKeyConstraint, UniqueConstraint
from sqlalchemy.dialects import postgresql

from novel_agent.db.models.outbox import OutboxEvent
from novel_agent.db.models.project import Project, StoryBranch
from novel_agent.projects.repository import _project_insert, _slugify
from novel_agent.projects.schemas import CreateProject


def _constraint_columns(constraint: UniqueConstraint) -> tuple[str, ...]:
    return tuple(column.name for column in constraint.columns)


def test_project_children_use_tenant_scoped_foreign_keys() -> None:
    project_uniques = {
        constraint.name: _constraint_columns(constraint)
        for constraint in Project.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
    }
    assert project_uniques["uq_projects_tenant_id"] == ("tenant_id", "id")

    for table in (StoryBranch.__table__, OutboxEvent.__table__):
        project_fks = [
            constraint
            for constraint in table.constraints
            if isinstance(constraint, ForeignKeyConstraint)
            and constraint.referred_table.name == "projects"
        ]
        assert len(project_fks) == 1
        assert tuple(column.name for column in project_fks[0].columns) == (
            "tenant_id",
            "project_id",
        )
        assert tuple(element.target_fullname for element in project_fks[0].elements) == (
            "projects.tenant_id",
            "projects.id",
        )


def test_outbox_uniqueness_is_tenant_scoped() -> None:
    unique = next(
        constraint
        for constraint in OutboxEvent.__table__.constraints
        if isinstance(constraint, UniqueConstraint)
        and constraint.name == "uq_outbox_events_tenant_aggregate_version"
    )
    assert _constraint_columns(unique) == (
        "tenant_id",
        "aggregate_type",
        "aggregate_id",
        "aggregate_version",
    )


def test_alembic_database_url_uses_environment_and_preserves_percent_encoding(
    monkeypatch,
) -> None:
    from novel_agent.db.migrations import configure_database_url

    database_url = "postgresql+asyncpg://user:p%40ss%25word@db.example/novel"
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config("alembic.ini")

    configure_database_url(config)

    assert config.get_main_option("sqlalchemy.url") == database_url


def test_project_insert_uses_request_id_conflict_target_and_returning() -> None:
    request = CreateProject(request_id="request-1", title="长夜")
    statement = _project_insert(uuid.uuid4(), request)
    sql = str(statement.compile(dialect=postgresql.dialect()))

    assert "ON CONFLICT ON CONSTRAINT uq_projects_tenant_creation_request DO NOTHING" in sql
    assert "RETURNING projects." in sql


def test_slug_is_bounded_disambiguated_and_unicode_stable() -> None:
    long_title = "长" * 200
    first = _slugify(long_title, "request-1")

    assert len(first) <= 100
    assert first == _slugify(long_title, "request-1")
    assert first != _slugify(long_title, "request-2")
    assert _slugify("！！！", "request-1").startswith("project-")
    assert _slugify("Ｆｏｏ　长夜", "request-1") == _slugify("foo 长夜", "request-1")
