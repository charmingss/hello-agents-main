from functools import lru_cache
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, StringConstraints, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

VersionToken = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True, min_length=1, max_length=64, pattern=r"^[a-zA-Z0-9._-]+$"
    ),
]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_nested_delimiter="__", extra="ignore", frozen=True
    )

    app_name: str = "AI Novel Agent"
    environment: Literal["development", "test", "production"] = "development"
    database_url: str = "postgresql+asyncpg://novel:novel@localhost:5432/novel"
    temporal_target: str = "localhost:7233"
    temporal_namespace: str = "default"
    temporal_task_queue: str = "novel-agent"
    redis_host: str = "localhost"
    redis_port: int = 6379
    minio_health_url: str = "http://localhost:9000/minio/health/live"
    minio_endpoint_url: str | None = None
    minio_access_key: SecretStr | None = None
    minio_secret_key: SecretStr | None = None
    minio_bucket: str | None = None
    minio_receipt_signing_key: SecretStr | None = None
    minio_retention_verification_key: SecretStr | None = None
    minio_allow_insecure_local: bool = False
    qdrant_health_url: str = "http://localhost:6333/healthz"
    neo4j_health_url: str = "http://localhost:7474/"
    neo4j_uri: str = "bolt://localhost:7687"
    neo4j_user: str = "neo4j"
    neo4j_password: SecretStr | None = None
    neo4j_database: str = "novel"
    cors_origins: tuple[str, ...] = ("http://localhost:3000",)
    cors_allow_credentials: bool = True
    oidc_issuer: str = "http://localhost:8080/realms/novel"
    oidc_audience: str = "novel-api"
    max_upload_bytes: Annotated[int, Field(gt=0, le=10 * 1024**3)] = 100 * 1024**2
    max_extracted_bytes: Annotated[int, Field(gt=0, le=20 * 1024**3)] = 250 * 1024**2
    max_pdf_pages: Annotated[int, Field(gt=0, le=10_000)] = 2_000
    max_archive_entries: Annotated[int, Field(gt=0, le=100_000)] = 10_000
    max_archive_entry_bytes: Annotated[int, Field(gt=0, le=20 * 1024**3)] = 100 * 1024**2
    max_compression_ratio: Annotated[int, Field(gt=0, le=10_000)] = 100
    upload_intent_ttl_seconds: Annotated[int, Field(gt=0, le=86_400)] = 900
    upload_validation_lease_seconds: Annotated[int, Field(gt=0, le=3_600)] = 300
    max_pending_upload_sessions_per_project: Annotated[int, Field(gt=0, le=10_000)] = 20
    parser_timeout_seconds: Annotated[int, Field(gt=0, le=3_600)] = 300
    parser_version: VersionToken = "builtin-v1"
    chunker_version: VersionToken = "novel-v1"
    analysis_provider: Literal["deepseek"] = "deepseek"
    analysis_base_url: str = "https://api.deepseek.com"
    analysis_model: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] = ""
    analysis_api_key: SecretStr | None = None
    analysis_timeout_seconds: Annotated[float, Field(gt=0, allow_inf_nan=False)] = 60
    extraction_model: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] = ""
    extraction_api_key: SecretStr | None = None
    extraction_base_url: str = ""
    outline_model: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] = ""
    outline_api_key: SecretStr | None = None
    outline_base_url: str = ""
    chapter_model: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] = ""
    chapter_api_key: SecretStr | None = None
    chapter_base_url: str = ""
    memory_model: Annotated[str, StringConstraints(strip_whitespace=True, max_length=200)] = ""
    memory_api_key: SecretStr | None = None
    memory_base_url: str = ""
    rag_enabled: bool = False
    rag_top_k: Annotated[int, Field(ge=1, le=20)] = 5

    @field_validator("analysis_timeout_seconds", mode="before")
    @classmethod
    def reject_boolean_analysis_timeout(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("analysis timeout must not be boolean")
        return value

    @field_validator("analysis_base_url")
    @classmethod
    def validate_analysis_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        _ = parsed.port
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(char.isspace() for char in value)
            or "\\" in value
        ):
            raise ValueError("analysis URL must be HTTPS without userinfo, query or fragment")
        return value.rstrip("/")

    @field_validator("extraction_base_url")
    @classmethod
    def validate_extraction_url(cls, value: str) -> str:
        if not value:
            return ""
        parsed = urlsplit(value)
        _ = parsed.port
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(char.isspace() for char in value)
            or "\\" in value
        ):
            raise ValueError("extraction URL must be HTTPS without userinfo, query or fragment")
        return value.rstrip("/")

    @field_validator("outline_base_url")
    @classmethod
    def validate_outline_url(cls, value: str) -> str:
        if not value:
            return ""
        parsed = urlsplit(value)
        _ = parsed.port
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(char.isspace() for char in value)
            or "\\" in value
        ):
            raise ValueError("outline URL must be HTTPS without userinfo, query or fragment")
        return value.rstrip("/")

    @field_validator("chapter_base_url")
    @classmethod
    def validate_chapter_url(cls, value: str) -> str:
        if not value:
            return ""
        parsed = urlsplit(value)
        _ = parsed.port
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(char.isspace() for char in value)
            or "\\" in value
        ):
            raise ValueError("chapter URL must be HTTPS without userinfo, query or fragment")
        return value.rstrip("/")

    @field_validator("memory_base_url")
    @classmethod
    def validate_memory_url(cls, value: str) -> str:
        if not value:
            return ""
        parsed = urlsplit(value)
        _ = parsed.port
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or any(char.isspace() for char in value)
            or "\\" in value
        ):
            raise ValueError("memory URL must be HTTPS without userinfo, query or fragment")
        return value.rstrip("/")

    @field_validator(
        "max_upload_bytes",
        "max_extracted_bytes",
        "max_pdf_pages",
        "max_archive_entries",
        "max_archive_entry_bytes",
        "max_compression_ratio",
        "upload_intent_ttl_seconds",
        "upload_validation_lease_seconds",
        "max_pending_upload_sessions_per_project",
        "parser_timeout_seconds",
        mode="before",
    )
    @classmethod
    def reject_boolean_ingestion_budgets(cls, value: object) -> object:
        if isinstance(value, bool):
            raise ValueError("ingestion security budgets must be integers, not booleans")
        return value

    @model_validator(mode="after")
    def reject_unsafe_cors(self) -> "Settings":
        if self.max_extracted_bytes < self.max_upload_bytes:
            raise ValueError("max_extracted_bytes must be at least max_upload_bytes")
        if self.cors_allow_credentials and "*" in self.cors_origins:
            raise ValueError("credentialed CORS requires explicit origins")
        for url in (
            self.minio_health_url,
            self.qdrant_health_url,
            self.neo4j_health_url,
        ):
            try:
                parsed = urlsplit(url)
                _ = parsed.port
            except ValueError as exc:
                raise ValueError("health URL must be an HTTP(S) URL without userinfo") from exc
            if (
                parsed.scheme not in {"http", "https"}
                or parsed.hostname is None
                or parsed.username is not None
                or parsed.password is not None
            ):
                raise ValueError("health URL must be an HTTP(S) URL without userinfo")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
