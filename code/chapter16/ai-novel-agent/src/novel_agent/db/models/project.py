import uuid

from sqlalchemy import (
    Boolean,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
    false,
)
from sqlalchemy.orm import Mapped, mapped_column

from novel_agent.db.base import Base


class Project(Base):
    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("tenant_id", "slug", name="uq_projects_tenant_slug"),
        UniqueConstraint("tenant_id", "id", name="uq_projects_tenant_id"),
        UniqueConstraint(
            "tenant_id", "creation_request_id", name="uq_projects_tenant_creation_request"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id"), index=True)
    creation_request_id: Mapped[str] = mapped_column(String(100))
    slug: Mapped[str] = mapped_column(String(100))
    title: Mapped[str] = mapped_column(String(200))
    language: Mapped[str] = mapped_column(String(20), default="zh-CN", server_default="zh-CN")
    status: Mapped[str] = mapped_column(String(30), default="active", server_default="active")


class StoryBranch(Base):
    __tablename__ = "story_branches"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "project_id"],
            ["projects.tenant_id", "projects.id"],
            name="fk_story_branches_tenant_project",
        ),
        UniqueConstraint(
            "tenant_id", "project_id", "name", name="uq_story_branches_tenant_project_name"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    tenant_id: Mapped[uuid.UUID] = mapped_column(Uuid, ForeignKey("tenants.id"), index=True)
    project_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    name: Mapped[str] = mapped_column(String(100))
    is_default: Mapped[bool] = mapped_column(Boolean, default=False, server_default=false())
