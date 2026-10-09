from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from novel_agent.api.routes import health
from novel_agent.api.routes.health import InfrastructureReadinessProbe
from novel_agent.config import Settings
from novel_agent.main import create_app


class FakeSession:
    def __init__(self, execute: AsyncMock | None = None) -> None:
        self.execute = execute or AsyncMock()
        self.entered = False
        self.exited = False

    async def __aenter__(self) -> FakeSession:
        self.entered = True
        return self

    async def __aexit__(self, *_: object) -> None:
        self.exited = True


class FakeSessionFactory:
    def __init__(self, session: FakeSession) -> None:
        self.session = session

    def __call__(self) -> FakeSession:
        return self.session


class FakeTemporalProvider:
    def __init__(self, healthy: bool = True) -> None:
        self.check_health = AsyncMock(return_value=healthy)
        self.client = SimpleNamespace(
            service_client=SimpleNamespace(check_health=self.check_health)
        )

    async def get(self) -> object:
        return self.client


def _probe(
    *,
    session: FakeSession | None = None,
    temporal: FakeTemporalProvider | None = None,
    http_client: httpx.AsyncClient | None = None,
    timeout: float = 0.05,
) -> InfrastructureReadinessProbe:
    return InfrastructureReadinessProbe(
        Settings(environment="test"),
        FakeSessionFactory(session or FakeSession()),  # type: ignore[arg-type]
        temporal or FakeTemporalProvider(),  # type: ignore[arg-type]
        http_client=http_client,
        timeout_seconds=timeout,
    )


@pytest.mark.asyncio
async def test_postgres_probe_executes_select_and_releases_session() -> None:
    session = FakeSession()
    probe = _probe(session=session)
    await probe.check("postgres")
    assert session.entered is True
    assert session.exited is True
    assert str(session.execute.await_args.args[0]) == "SELECT 1"
    await probe.aclose()


@pytest.mark.asyncio
async def test_temporal_probe_uses_the_configured_operation_budget() -> None:
    temporal = FakeTemporalProvider()
    probe = _probe(temporal=temporal, timeout=0.025)
    await probe.check("temporal")
    assert temporal.check_health.await_args.kwargs["timeout"] == timedelta(seconds=0.025)
    await probe.aclose()


class FakeReader:
    async def readline(self) -> bytes:
        return b"+PONG\r\n"


class FakeWriter:
    def __init__(self) -> None:
        self.payload = b""
        self.closed = False

    def write(self, payload: bytes) -> None:
        self.payload = payload

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_redis_probe_sends_resp_ping_and_closes_transport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer = FakeWriter()
    open_connection = AsyncMock(return_value=(FakeReader(), writer))
    monkeypatch.setattr(health.asyncio, "open_connection", open_connection)
    probe = _probe()
    await probe.check("redis")
    open_connection.assert_awaited_once_with("localhost", 6379)
    assert writer.payload == b"*1\r\n$4\r\nPING\r\n"
    assert writer.closed is True
    await probe.aclose()


@pytest.mark.asyncio
async def test_http_probes_use_expected_urls_and_surface_bad_status() -> None:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(503 if request.url.path == "/healthz" else 200)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    probe = _probe(http_client=client)
    await probe.check("minio")
    with pytest.raises(httpx.HTTPStatusError):
        await probe.check("qdrant")
    await probe.check("neo4j")
    assert requested == [
        "http://localhost:9000/minio/health/live",
        "http://localhost:6333/healthz",
        "http://localhost:7474/",
    ]
    await probe.aclose()
    assert client.is_closed is True


@pytest.mark.asyncio
async def test_nonresponsive_operation_is_bounded_by_probe_budget() -> None:
    async def never_returns(*_: object) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            # Simulate a transport cleanup path that is slow to acknowledge cancellation.
            await asyncio.sleep(0.2)

    session = FakeSession(AsyncMock(side_effect=never_returns))
    probe = _probe(session=session, timeout=0.01)
    started = asyncio.get_running_loop().time()
    with pytest.raises(TimeoutError):
        await probe.check("postgres")
    elapsed = asyncio.get_running_loop().time() - started
    assert elapsed < 0.1
    await probe.aclose()
    await asyncio.sleep(0.25)
    assert session.exited is True


@pytest.mark.asyncio
async def test_timed_out_checks_share_one_inflight_task_and_consume_its_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()
    attempts = 0
    loop_errors: list[dict[str, object]] = []
    loop = asyncio.get_running_loop()
    previous_handler = loop.get_exception_handler()
    loop.set_exception_handler(lambda _loop, context: loop_errors.append(context))

    async def slow_failure(_: object) -> None:
        nonlocal attempts
        attempts += 1
        await release.wait()
        raise RuntimeError("consumed probe failure")

    probe = _probe(timeout=0.01)
    monkeypatch.setattr(probe, "_check", slow_failure)
    try:
        results = await asyncio.gather(
            *(probe.check("postgres") for _ in range(20)), return_exceptions=True
        )
        assert all(isinstance(result, TimeoutError) for result in results)
        assert attempts == 1

        for _ in range(5):
            with pytest.raises(TimeoutError):
                await probe.check("postgres")
        assert attempts == 1

        release.set()
        await asyncio.sleep(0.01)
        assert loop_errors == []
    finally:
        loop.set_exception_handler(previous_handler)
        await probe.aclose()


@pytest.mark.asyncio
async def test_probe_close_is_bounded_when_inflight_ignores_first_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    release = asyncio.Event()
    attempts = 0

    async def cancellation_resistant(_: object) -> None:
        nonlocal attempts
        attempts += 1
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            await release.wait()

    probe = _probe(timeout=0.01)
    monkeypatch.setattr(probe, "_check", cancellation_resistant)
    with pytest.raises(TimeoutError):
        await probe.check("neo4j")

    started = asyncio.get_running_loop().time()
    await probe.aclose()
    assert asyncio.get_running_loop().time() - started < 0.1
    assert attempts == 1
    release.set()
    await asyncio.sleep(0.01)


def test_app_lifespan_wires_real_probe_and_closes_owned_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    check = AsyncMock()
    monkeypatch.setattr(InfrastructureReadinessProbe, "check", check)
    app = create_app(Settings(environment="test"))
    with TestClient(app) as client:
        probe = app.state.readiness_probe
        response = client.get("/api/v1/health/ready")
        assert isinstance(probe, InfrastructureReadinessProbe)
        assert probe.is_closed is False
    assert response.status_code == 200
    assert check.await_count == 6
    assert probe.is_closed is True
