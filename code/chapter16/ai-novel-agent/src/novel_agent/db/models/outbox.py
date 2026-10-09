import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from novel_agent.db.base import Base


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_outbox_events_tenant_project",
        ),
        UniqueConstraint(
            "tenant_id",
            "aggregate_type",
            "aggregate_id",
            "aggregate_version",
            name="uq_outbox_events_tenant_aggregate_version",
        ),
        CheckConstraint(
            "(lease_token IS NULL AND lease_owner IS NULL AND lease_expires_at IS NULL) OR "
            "(lease_token IS NOT NULL AND lease_owner IS NOT NULL "
            "AND length(btrim(lease_owner)) > 0 "
            "AND lease_expires_at IS NOT NULL)",
            name="ck_outbox_projection_lease",
        ),
        CheckConstraint(
            "(dead_at IS NULL AND dead_reason IS NULL) OR "
            "(dead_at IS NOT NULL AND dead_reason IS NOT NULL "
            "AND dead_reason ~ '^[a-z][a-z0-9_]{0,63}$')",
            name="ck_outbox_dead_letter",
        ),
        Index(
            "ix_outbox_projection_claim",
            "published_at",
            "dead_at",
            "next_attempt_at",
            "occurred_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id"), index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    aggregate_type: Mapped[str] = mapped_column(String(100))
    aggregate_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    event_type: Mapped[str] = mapped_column(String(150))
    aggregate_version: Mapped[int] = mapped_column(Integer)
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(100), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dead_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    dead_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
