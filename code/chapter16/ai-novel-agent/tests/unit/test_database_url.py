import pytest
from conftest import postgres_test_database_url, require_external_database_enabled


def test_postgres_test_database_url_prefers_environment_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    override = "postgresql+asyncpg://override:secret@db.example.test/novel_test"
    monkeypatch.setenv("TEST_DATABASE_URL", override)

    assert postgres_test_database_url() == override


def test_external_database_gate_requires_opt_in_and_explicit_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RUN_EXTERNAL_TESTS", raising=False)
    monkeypatch.delenv("TEST_DATABASE_URL", raising=False)
    with pytest.raises(RuntimeError, match="RUN_EXTERNAL_TESTS"):
        require_external_database_enabled()

    monkeypatch.setenv("RUN_EXTERNAL_TESTS", "1")
    with pytest.raises(RuntimeError, match="TEST_DATABASE_URL"):
        require_external_database_enabled()
