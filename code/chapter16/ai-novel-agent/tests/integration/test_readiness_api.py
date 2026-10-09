from __future__ import annotations

import asyncio
from collections.abc import Mapping
from typing import Literal

import pytest
from fastapi.testclient import TestClient

from novel_agent.api.routes import health
from novel_agent.api.routes.health import get_readiness_probe
from novel_agent.config import Settings
from novel_agent.main import create_app

DependencyName = Literal["postgres", "temporal", "redis", "minio", "qdrant", "neo4j"]
DEPENDENCIES: tuple[DependencyName, ...] = (
    "postgres",
    "temporal",
    "redis",
    "minio",
    "qdrant",
    "neo4j",
)


class FakeReadinessProbe:
    def __init__(
        self,
        availability: Mapping[DependencyName, bool] | None = None,
        *,
        error: Exception | None = None,
        delay: float = 0,
        barrier: asyncio.Event | None = None,
    ) -> None:
        self.availability = {name: True for name in DEPENDENCIES}
        self.availability.update(availability or {})
        self.error = error
        self.delay = delay
        self.barrier = barrier
        self.started: set[DependencyName] = set()

    async def check(self, dependency: DependencyName) -> None:
        self.started.add(dependency)
        if self.barrier is not None:
            if self.started == set(DEPENDENCIES):
                self.barrier.set()
            await self.barrier.wait()
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error
        if not self.availability[dependency]:
            raise ConnectionError("dependency is down")


def _client(probe: FakeReadinessProbe) -> TestClient:
    app = create_app(Settings(environment="test"))
    app.dependency_overrides[get_readiness_probe] = lambda: probe
    return TestClient(app, raise_server_exceptions=False)


def test_readiness_is_ready_when_every_dependency_is_available() -> None:
    with _client(FakeReadinessProbe()) as client:
        response = client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json() == {
        "status": "ready",
        "dependencies": {name: "available" for name in DEPENDENCIES},
    }


@pytest.mark.parametrize("required", ["postgres", "temporal"])
def test_readiness_is_unavailable_when_a_required_dependency_is_down(
    required: DependencyName,
) -> None:
    with _client(FakeReadinessProbe({required: False})) as client:
        response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert response.json()["dependencies"][required] == "unavailable"


@pytest.mark.parametrize("optional", ["redis", "minio", "qdrant", "neo4j"])
def test_readiness_is_degraded_when_an_optional_dependency_is_down(
    optional: DependencyName,
) -> None:
    with _client(FakeReadinessProbe({optional: False})) as client:
        response = client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "degraded"
    assert response.json()["dependencies"][optional] == "unavailable"


def test_readiness_normalizes_probe_exceptions_without_leaking_details() -> None:
    secret = "postgresql://admin:do-not-leak@db/novel"
    with _client(FakeReadinessProbe(error=RuntimeError(secret))) as client:
        response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert response.json()["status"] == "unavailable"
    assert set(response.json()["dependencies"].values()) == {"unavailable"}
    assert secret not in response.text
    assert "RuntimeError" not in response.text


def test_readiness_bounds_each_dependency_check(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(health, "READINESS_CHECK_TIMEOUT_SECONDS", 0.01)
    with _client(FakeReadinessProbe(delay=0.1)) as client:
        response = client.get("/api/v1/health/ready")

    assert response.status_code == 503
    assert set(response.json()["dependencies"].values()) == {"unavailable"}


def test_readiness_checks_dependencies_concurrently() -> None:
    probe = FakeReadinessProbe(barrier=asyncio.Event())
    with _client(probe) as client:
        response = client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert probe.started == set(DEPENDENCIES)


def test_readiness_does_not_change_liveness_semantics() -> None:
    with _client(FakeReadinessProbe(error=RuntimeError("all dependencies down"))) as client:
        response = client.get("/api/v1/health/live")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}
