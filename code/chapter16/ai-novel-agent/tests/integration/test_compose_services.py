from pathlib import Path
from typing import Any

import yaml


PROJECT_ROOT = Path(__file__).parents[2]
EXPECTED_SERVICES = {"postgres", "redis", "minio", "qdrant", "neo4j", "temporal"}
EXPECTED_IMAGES = {
    "postgres": "postgres:17.11-alpine3.24",
    "redis": "redis:7.4.11-alpine3.21",
    "minio": "minio/minio:RELEASE.2025-07-23T15-54-02Z",
    "qdrant": "qdrant/qdrant:v1.19.0",
    "neo4j": "neo4j:2026.07.1-community",
    "temporal": "temporalio/auto-setup:1.29.6",
}
SUPPORTED_TEMPORAL_DATABASES = {"postgres12", "postgres12_pgx", "mysql8", "cassandra"}


def load_compose() -> dict[str, Any]:
    with (PROJECT_ROOT / "compose.yaml").open(encoding="utf-8") as compose_file:
        return yaml.safe_load(compose_file)


def test_compose_defines_required_services() -> None:
    services = load_compose()["services"]

    assert EXPECTED_SERVICES <= services.keys()


def test_service_images_use_pinned_release_tags() -> None:
    services = load_compose()["services"]

    assert {name: services[name]["image"] for name in EXPECTED_SERVICES} == EXPECTED_IMAGES
    assert all(not image.endswith(":latest") for image in EXPECTED_IMAGES.values())


def test_postgres_service_contract() -> None:
    postgres = load_compose()["services"]["postgres"]

    assert postgres["environment"] == {
        "POSTGRES_DB": "novel",
        "POSTGRES_USER": "novel",
        "POSTGRES_PASSWORD": "novel",
    }
    assert postgres["ports"] == ["127.0.0.1:${POSTGRES_PORT:-5432}:5432"]
    assert postgres["volumes"] == ["postgres_data:/var/lib/postgresql/data"]
    assert postgres["healthcheck"] == {
        "test": ["CMD-SHELL", "pg_isready -U novel -d novel"],
        "interval": "5s",
        "timeout": "3s",
        "retries": 20,
    }


def test_redis_service_contract() -> None:
    redis = load_compose()["services"]["redis"]

    assert redis["ports"] == ["127.0.0.1:${REDIS_PORT:-6379}:6379"]
    assert redis["healthcheck"] == {
        "test": ["CMD", "redis-cli", "ping"],
        "interval": "5s",
        "timeout": "3s",
        "retries": 20,
    }


def test_minio_service_contract() -> None:
    minio = load_compose()["services"]["minio"]

    assert minio["command"] == 'server /data --console-address ":9001"'
    assert minio["environment"] == {
        "MINIO_ROOT_USER": "novel",
        "MINIO_ROOT_PASSWORD": "novel-local-only",
    }
    assert minio["ports"] == [
        "127.0.0.1:${MINIO_API_PORT:-9000}:9000",
        "127.0.0.1:${MINIO_CONSOLE_PORT:-9001}:9001",
    ]
    assert minio["volumes"] == ["minio_data:/data"]


def test_qdrant_service_contract() -> None:
    qdrant = load_compose()["services"]["qdrant"]

    assert qdrant["ports"] == [
        "127.0.0.1:${QDRANT_HTTP_PORT:-6333}:6333",
        "127.0.0.1:${QDRANT_GRPC_PORT:-6334}:6334",
    ]
    assert qdrant["volumes"] == ["qdrant_data:/qdrant/storage"]


def test_neo4j_service_contract() -> None:
    neo4j = load_compose()["services"]["neo4j"]

    assert neo4j["environment"] == {"NEO4J_AUTH": "neo4j/novel-local-only"}
    assert neo4j["ports"] == [
        "127.0.0.1:${NEO4J_HTTP_PORT:-7474}:7474",
        "127.0.0.1:${NEO4J_BOLT_PORT:-7687}:7687",
    ]
    assert neo4j["volumes"] == ["neo4j_data:/data"]


def test_compose_defines_persistent_named_volumes() -> None:
    volumes = load_compose()["volumes"]

    assert set(volumes) == {"postgres_data", "minio_data", "qdrant_data", "neo4j_data"}


def test_temporal_service_contract() -> None:
    temporal = load_compose()["services"]["temporal"]

    assert temporal["environment"] == {
        "DB": "postgres12",
        "DB_PORT": 5432,
        "DBNAME": "temporal",
        "POSTGRES_USER": "novel",
        "POSTGRES_PWD": "novel",
        "POSTGRES_SEEDS": "postgres",
    }
    assert temporal["environment"]["DB"] in SUPPORTED_TEMPORAL_DATABASES
    assert temporal["ports"] == ["127.0.0.1:${TEMPORAL_PORT:-7233}:7233"]
    assert temporal["depends_on"] == {"postgres": {"condition": "service_healthy"}}
