from __future__ import annotations

import asyncio
from collections.abc import Awaitable
from datetime import timedelta
from functools import partial
from typing import Annotated, Literal, Protocol

import httpx
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from novel_agent.config import Settings
from novel_agent.workflows.client import TemporalClientProvider

router = APIRouter(prefix="/health", tags=["health"])

DependencyName = Literal["postgres", "temporal", "redis", "minio", "qdrant", "neo4j"]
DependencyStatus = Literal["available", "unavailable"]
ReadinessStatus = Literal["ready", "degraded", "unavailable"]

DEPENDENCIES: tuple[DependencyName, ...] = (
    "postgres",
    "temporal",
    "redis",
    "minio",
    "qdrant",
    "neo4j",
)
REQUIRED_DEPENDENCIES: frozenset[DependencyName] = frozenset({"postgres", "temporal"})
READINESS_CHECK_TIMEOUT_SECONDS = 2.0


def _consume_background_result(task: asyncio.Task[object]) -> None:
    if task.cancelled():
        return
    task.exception()


async def _run_with_budget[ResultT](
    awaitable: Awaitable[ResultT], budget_seconds: float
) -> ResultT:
    """Return at the deadline even when a transport delays cancellation cleanup."""

    task = asyncio.ensure_future(awaitable)
    try:
        done, _ = await asyncio.wait({task}, timeout=budget_seconds)
    except asyncio.CancelledError:
        task.cancel()
        task.add_done_callback(_consume_background_result)
        raise
    if task not in done:
        task.cancel()
        task.add_done_callback(_consume_background_result)
        raise TimeoutError
    return task.result()


class ReadinessProbe(Protocol):
    async def check(self, dependency: DependencyName) -> None: ...


class InfrastructureReadinessProbe:
    """Owns reusable transport resources and bounds every infrastructure probe."""

    def __init__(
        self,
        settings: Settings,
        session_factory: async_sessionmaker[AsyncSession],
        temporal_provider: TemporalClientProvider,
        *,
        http_client: httpx.AsyncClient | None = None,
        timeout_seconds: float = READINESS_CHECK_TIMEOUT_SECONDS,
    ) -> None:
        self._settings = settings
        self._session_factory = session_factory
        self._temporal_provider = temporal_provider
        self._timeout_seconds = timeout_seconds
        self._http_client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(timeout_seconds))
        self._inflight: dict[DependencyName, asyncio.Task[None]] = {}
        self._closed = False

    @property
    def is_closed(self) -> bool:
        return self._http_client.is_closed

    async def aclose(self) -> None:
        self._closed = True
        for task in tuple(self._inflight.values()):
            task.cancel()
        try:
            await _run_with_budget(self._http_client.aclose(), self._timeout_seconds)
        except TimeoutError:
            pass

    async def check(self, dependency: DependencyName) -> None:
        if self._closed:
            raise RuntimeError("readiness probe is closed")
        task = self._inflight.get(dependency)
        if task is None:
            task = asyncio.create_task(self._check(dependency))
            self._inflight[dependency] = task
            task.add_done_callback(partial(self._finish_check, dependency))
        done, _ = await asyncio.wait({task}, timeout=self._timeout_seconds)
        if task not in done:
            raise TimeoutError
        task.result()

    def _finish_check(self, dependency: DependencyName, task: asyncio.Task[None]) -> None:
        if self._inflight.get(dependency) is task:
            del self._inflight[dependency]
        if not task.cancelled():
            task.exception()

    async def _check(self, dependency: DependencyName) -> None:
        if dependency == "postgres":
            await self._check_postgres()
        elif dependency == "temporal":
            await self._check_temporal()
        elif dependency == "redis":
            await self._check_redis()
        else:
            await self._check_http(self._health_url(dependency))

    async def _check_postgres(self) -> None:
        async with self._session_factory() as session:
            await session.execute(text("SELECT 1"))

    async def _check_temporal(self) -> None:
        client = await self._temporal_provider.get()
        healthy = await client.service_client.check_health(
            timeout=timedelta(seconds=self._timeout_seconds)
        )
        if not healthy:
            raise ConnectionError("Temporal health check failed")

    async def _check_redis(self) -> None:
        reader, writer = await asyncio.open_connection(
            self._settings.redis_host, self._settings.redis_port
        )
        try:
            writer.write(b"*1\r\n$4\r\nPING\r\n")
            await writer.drain()
            response = await reader.readline()
            if response != b"+PONG\r\n":
                raise ConnectionError("Redis health check failed")
        finally:
            writer.close()

    async def _check_http(self, url: str) -> None:
        response = await self._http_client.get(url, timeout=httpx.Timeout(self._timeout_seconds))
        response.raise_for_status()

    def _health_url(self, dependency: DependencyName) -> str:
        if dependency == "minio":
            return self._settings.minio_health_url
        if dependency == "qdrant":
            return self._settings.qdrant_health_url
        if dependency == "neo4j":
            return self._settings.neo4j_health_url
        raise ValueError("dependency does not use HTTP health checks")


def get_readiness_probe(request: Request) -> ReadinessProbe:
    try:
        probe: ReadinessProbe = request.app.state.readiness_probe
    except AttributeError:
        raise RuntimeError(
            "readiness probe is not configured; application lifespan did not run"
        ) from None
    return probe


async def _dependency_status(probe: ReadinessProbe, dependency: DependencyName) -> DependencyStatus:
    try:
        await _run_with_budget(probe.check(dependency), READINESS_CHECK_TIMEOUT_SECONDS)
    except Exception:
        return "unavailable"
    return "available"


@router.get("/live")
async def live() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/ready")
async def ready(
    probe: Annotated[ReadinessProbe, Depends(get_readiness_probe)],
) -> JSONResponse:
    results = await asyncio.gather(
        *(_dependency_status(probe, dependency) for dependency in DEPENDENCIES)
    )
    dependencies = dict(zip(DEPENDENCIES, results, strict=True))
    required_down = any(
        dependencies[dependency] == "unavailable" for dependency in REQUIRED_DEPENDENCIES
    )
    optional_down = any(
        status == "unavailable"
        for dependency, status in dependencies.items()
        if dependency not in REQUIRED_DEPENDENCIES
    )
    status: ReadinessStatus
    status_code: int
    if required_down:
        status, status_code = "unavailable", 503
    elif optional_down:
        status, status_code = "degraded", 200
    else:
        status, status_code = "ready", 200
    return JSONResponse(
        status_code=status_code,
        content={"status": status, "dependencies": dependencies},
    )
