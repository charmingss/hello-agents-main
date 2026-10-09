from dataclasses import dataclass
from typing import Any, cast

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.dml import ReturningInsert

from novel_agent.db.models.outbox import OutboxEvent
from novel_agent.outbox.errors import OutboxConflictError
from novel_agent.outbox.events import IntegrationEvent


@dataclass(frozen=True)
class OutboxWriteResult:
    value: OutboxEvent
    created: bool


def _outbox_values(event: IntegrationEvent) -> dict[str, Any]:
    payload = event.model_dump(mode="json")
    return {
        "id": event.event_id,
        "tenant_id": event.tenant_id,
        "project_id": event.project_id,
        "aggregate_type": event.aggregate_type,
        "aggregate_id": event.aggregate_id,
        "aggregate_version": event.aggregate_version,
        "event_type": event.event_type,
        "payload": payload,
        "occurred_at": event.occurred_at,
    }


def _outbox_insert(event: IntegrationEvent) -> ReturningInsert[tuple[OutboxEvent]]:
    return (
        insert(OutboxEvent)
        .values(**_outbox_values(event))
        .on_conflict_do_nothing()
        .returning(OutboxEvent)
    )


class SqlAlchemyOutboxRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, event: IntegrationEvent) -> OutboxWriteResult:
        created = cast(OutboxEvent | None, await self._session.scalar(_outbox_insert(event)))
        if created is not None:
            return OutboxWriteResult(created, True)
        existing = cast(
            OutboxEvent | None,
            await self._session.scalar(
                select(OutboxEvent).where(
                    or_(
                        OutboxEvent.id == event.event_id,
                        (
                            (OutboxEvent.tenant_id == event.tenant_id)
                            & (OutboxEvent.aggregate_type == event.aggregate_type)
                            & (OutboxEvent.aggregate_id == event.aggregate_id)
                            & (OutboxEvent.aggregate_version == event.aggregate_version)
                        ),
                    )
                )
            ),
        )
        if existing is None:
            raise OutboxConflictError("outbox conflict resolved without a visible winning row")
        expected = _outbox_values(event)
        if any(getattr(existing, key) != value for key, value in expected.items()):
            raise OutboxConflictError("outbox event identity conflicts with immutable payload")
        return OutboxWriteResult(existing, False)


__all__ = ["OutboxWriteResult", "SqlAlchemyOutboxRepository"]
