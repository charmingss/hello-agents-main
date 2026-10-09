from __future__ import annotations

import asyncio
import math
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING

from novel_agent.outbox.projector import ProjectionLeaseLost

if TYPE_CHECKING:
    from novel_agent.config import Settings
    from novel_agent.embeddings.ports import EmbeddingProvider
    from novel_agent.outbox.projector import OutboxProjectionRunner, VectorProjection


async def poll_projection(
    runner: OutboxProjectionRunner,
    *,
    stop_event: asyncio.Event,
    poll_interval_seconds: float = 1.0,
) -> None:
    """Poll sequentially until stopped, finishing any in-flight run before returning."""
    if (
        isinstance(poll_interval_seconds, bool)
        or not math.isfinite(poll_interval_seconds)
        or poll_interval_seconds <= 0
    ):
        raise ValueError("poll interval must be finite and greater than zero")

    while not stop_event.is_set():
        try:
            worked = await runner.run_once()
        except ProjectionLeaseLost:
            worked = False
        if worked:
            await asyncio.sleep(0)
        else:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=poll_interval_seconds)
            except TimeoutError:
                pass


@asynccontextmanager
async def projection_runtime(
    settings: Settings,
    *,
    embedding_provider: EmbeddingProvider,
    projection: VectorProjection,
    owner: str,
    lease_for: timedelta = timedelta(minutes=5),
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> AsyncIterator[OutboxProjectionRunner]:
    """Own the database engine for explicit run_once calls; clients remain caller-owned."""
    from novel_agent.db.session import create_engine, create_session_factory
    from novel_agent.outbox.projector import (
        OutboxProjectionRunner,
        ProjectionWorker,
        SqlAlchemyOutboxLeases,
        SqlAlchemyProjectionAuthority,
        SqlAlchemyProjectionCheckpoints,
    )

    SqlAlchemyOutboxLeases._validate_owner(owner)
    if not isinstance(lease_for, timedelta) or lease_for <= timedelta(0):
        raise ValueError("valid projection runner lease settings are required")
    if projection.embedding != embedding_provider.identity:
        raise ValueError("projection and provider embedding identities differ")

    engine = create_engine(settings)
    try:
        session_factory = create_session_factory(engine)
        worker = ProjectionWorker(
            SqlAlchemyProjectionAuthority(session_factory),
            embedding_provider,
            projection,
            SqlAlchemyProjectionCheckpoints(session_factory, clock=clock),
        )
        yield OutboxProjectionRunner(
            session_factory, lambda: worker, owner=owner, lease_for=lease_for, clock=clock,
        )
    finally:
        await engine.dispose()
