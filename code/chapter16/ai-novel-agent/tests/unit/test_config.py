import pytest
from pydantic import ValidationError

from novel_agent.config import Settings, get_settings
from novel_agent.main import create_app


def test_settings_reject_wildcard_cors_with_credentials() -> None:
    with pytest.raises(ValidationError):
        Settings(
            environment="production",
            database_url="postgresql+asyncpg://user:pass@db/app",
            temporal_target="temporal:7233",
            cors_origins=["*"],
            cors_allow_credentials=True,
        )


def test_settings_accept_explicit_production_origin() -> None:
    settings = Settings(
        environment="production",
        database_url="postgresql+asyncpg://user:pass@db/app",
        temporal_target="temporal:7233",
        cors_origins=["https://novel.example.com"],
        cors_allow_credentials=True,
    )
    assert settings.cors_origins == ("https://novel.example.com",)


def test_settings_cors_origins_cannot_be_mutated_in_place() -> None:
    settings = Settings(cors_origins=["https://novel.example.com"])

    with pytest.raises(TypeError):
        settings.cors_origins[0] = "*"


def test_get_settings_caches_same_instance() -> None:
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()


def test_create_app_uses_injected_settings_title() -> None:
    settings = Settings(app_name="Custom Novel Agent")

    app = create_app(settings)

    assert app.title == "Custom Novel Agent"


@pytest.mark.parametrize("field", ["minio_health_url", "qdrant_health_url", "neo4j_health_url"])
@pytest.mark.parametrize(
    "url",
    ["file:///etc/passwd", "ftp://localhost/health", "http://user:secret@localhost/health"],
)
def test_settings_reject_unsafe_health_urls(field: str, url: str) -> None:
    with pytest.raises(ValueError, match="health URL"):
        Settings(**{field: url})


@pytest.mark.parametrize("field", ["minio_health_url", "qdrant_health_url", "neo4j_health_url"])
def test_settings_reject_out_of_range_health_url_ports(field: str) -> None:
    with pytest.raises(ValueError, match="health URL"):
        Settings(**{field: "http://localhost:99999/health"})


@pytest.mark.parametrize(
    "field",
    [
        "max_upload_bytes",
        "max_extracted_bytes",
        "max_pdf_pages",
        "max_archive_entries",
        "parser_timeout_seconds",
    ],
)
@pytest.mark.parametrize("value", [0, -1])
def test_ingestion_security_budgets_cannot_be_disabled(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: value})


@pytest.mark.parametrize(
    "field",
    [
        "max_upload_bytes",
        "max_extracted_bytes",
        "max_pdf_pages",
        "max_archive_entries",
        "parser_timeout_seconds",
    ],
)
def test_ingestion_security_budgets_reject_python_booleans(field: str) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: True})


def test_ingestion_security_budgets_accept_integer_environment_strings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("MAX_UPLOAD_BYTES", "1024")
    monkeypatch.setenv("MAX_EXTRACTED_BYTES", "2048")
    monkeypatch.setenv("MAX_PDF_PAGES", "10")
    monkeypatch.setenv("MAX_ARCHIVE_ENTRIES", "20")
    monkeypatch.setenv("PARSER_TIMEOUT_SECONDS", "30")

    settings = Settings(_env_file=None)

    assert settings.max_upload_bytes == 1024
    assert settings.max_extracted_bytes == 2048
    assert settings.max_pdf_pages == 10
    assert settings.max_archive_entries == 20
    assert settings.parser_timeout_seconds == 30


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("max_upload_bytes", 10 * 1024**3 + 1),
        ("max_extracted_bytes", 20 * 1024**3 + 1),
        ("max_pdf_pages", 10_001),
        ("max_archive_entries", 100_001),
        ("parser_timeout_seconds", 3_601),
    ],
)
def test_ingestion_security_budgets_have_hard_upper_bounds(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_extracted_budget_cannot_be_smaller_than_upload_budget() -> None:
    with pytest.raises(ValidationError, match="max_extracted_bytes"):
        Settings(max_upload_bytes=2_000, max_extracted_bytes=1_000)


@pytest.mark.parametrize("field", ["parser_version", "chunker_version"])
@pytest.mark.parametrize("value", ["", " ", "version with spaces", "x" * 65])
def test_ingestion_versions_are_non_empty_bounded_tokens(field: str, value: str) -> None:
    with pytest.raises(ValidationError):
        Settings(**{field: value})


def test_ingestion_defaults_are_safe_and_settings_remain_frozen() -> None:
    settings = Settings()

    assert 0 < settings.max_upload_bytes <= settings.max_extracted_bytes
    assert settings.max_pdf_pages > 0
    assert settings.max_archive_entries > 0
    assert settings.parser_timeout_seconds > 0
    with pytest.raises(ValidationError):
        settings.max_upload_bytes = 0
