"""
Task: a unit of work an organization has defined, optionally against a
DataSource. Module 3 only models and CRUDs this — actually running a task
is Module 4's job.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
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
# NOTE: this must be sqlalchemy.dialects.postgresql.ENUM, NOT the generic
# sqlalchemy.Enum. The generic Enum silently ignores an unrecognized
# create_type kwarg (its __init__ never pops it), so create_type=False
# below would otherwise have no effect and Base.metadata.create_all()
# would independently emit CREATE TYPE against a live PostgreSQL database
# -- exactly the DuplicateObject bug hit during Module 4 verification.
# Only postgresql.ENUM genuinely implements and honors create_type.
from sqlalchemy.dialects.postgresql import ENUM as SAEnum
from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import TaskType

# create_type=False: see the identical comment on source_type_enum in
# app/models/data_source.py -- Alembic migrations own CREATE/DROP TYPE for
# every native PostgreSQL enum in this project; models must never issue it
# independently via Base.metadata.create_all().
task_type_enum = SAEnum(
    TaskType,
    name="task_type_enum",
    values_callable=lambda obj: [e.value for e in obj],
    create_constraint=True,
    create_type=False,
)


class Task(Base):
    __tablename__ = "tasks"
    __table_args__ = (
        # Required so TaskRun can have a composite FK (organization_id,
        # task_id) -> (organization_id, id). Same pattern as DataSource.
        UniqueConstraint("organization_id", "id", name="uq_tasks_org_id"),
        Index(
            "ix_tasks_org_name_active",
            "organization_id",
            text("lower(trim(name))"),
            unique=True,
            postgresql_where=text("is_active = true"),
            sqlite_where=text("is_active = 1"),
        ),
        # Tenant-aware composite FK: a Task's data_source_id (when set) MUST
        # belong to a DataSource in the SAME organization_id. Enforced by
        # PostgreSQL itself, not just application code. NULL data_source_id
        # is allowed (task with no source) — standard multi-column FK
        # semantics skip the check when any referencing column is NULL.
        ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_tasks_org_data_source",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    data_source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text(), nullable=True)
    task_type: Mapped[TaskType] = mapped_column(task_type_enum, nullable=False)
    schedule: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # --- Module 4: execution engine defaults (additive, nullable) ---
    # NULL means "use the worker's global default" (see app.core.config),
    # not "zero/unlimited" -- an explicit override is only ever a real
    # positive integer, validated at the schema layer.
    max_attempts: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    timeout_seconds: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    data_source: Mapped["DataSource | None"] = relationship(back_populates="tasks")  # noqa: F821
    runs: Mapped[list["TaskRun"]] = relationship(back_populates="task")  # noqa: F821

    def __repr__(self) -> str:
        return f"Task(id={self.id!r}, org={self.organization_id!r}, name={self.name!r})"
