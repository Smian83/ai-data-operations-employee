"""
Task: a unit of work an organization has defined, optionally against a
DataSource. Module 3 only models and CRUDs this — actually running a task
is Module 4's job.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
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
        # --- Module 12: scheduled task execution ---------------------------
        # Fixed, non-configurable safety floor -- deliberately independent of
        # app.core.config.Settings.minimum_schedule_interval_seconds (the
        # real, operator-facing, configurable minimum enforced at the
        # Pydantic layer on every write path). A database CHECK constraint
        # cannot read an environment variable, so this constant exists as a
        # defense-in-depth backstop only: it is reached only if the
        # application-layer check is ever bypassed or misconfigured, never
        # as the primary UX validation. See app/schemas/task.py and
        # docs/module-12-scheduled-task-execution-design.md.
        CheckConstraint(
            "schedule_interval_seconds IS NULL OR schedule_interval_seconds >= 30",
            name="ck_tasks_schedule_interval_hard_floor",
        ),
        # Both columns are always both-NULL or both-set together -- same
        # "no half-state" discipline as ck_task_runs_lease_consistency on
        # TaskRun (lease_token/lease_expires_at).
        CheckConstraint(
            "(schedule_interval_seconds IS NULL AND next_run_at IS NULL)"
            " OR (schedule_interval_seconds IS NOT NULL AND next_run_at IS NOT NULL)",
            name="ck_tasks_schedule_consistency",
        ),
        # Partial index for the scheduler's due-task poll query (see
        # app/worker/scheduler.py). Deliberately not organization-scoped --
        # the scheduler, like claim_batch, polls system-wide in one query
        # and relies on each row's own organization_id for tenant
        # correctness downstream, not on a filtered scan. `id` is a
        # deterministic secondary sort key so ties on next_run_at (e.g.
        # many tasks all overdue by the same wall-clock instant) still
        # produce a stable, starvation-free processing order.
        Index(
            "ix_tasks_scheduled_due",
            "next_run_at",
            "id",
            postgresql_where=text("schedule_interval_seconds IS NOT NULL AND is_active = true"),
            sqlite_where=text("schedule_interval_seconds IS NOT NULL AND is_active = 1"),
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
    # Deprecated as of Module 12 -- a free-text, non-executable label only.
    # Never read by the scheduler (app/worker/scheduler.py) and never
    # migrated/reinterpreted into schedule_interval_seconds below. Kept
    # read/write for backward compatibility; see
    # docs/module-12-scheduled-task-execution-design.md Section 5 for the
    # deprecation/removal strategy.
    schedule: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # --- Module 12: scheduled task execution (additive, nullable) ---
    # The sole authoritative, machine-executable scheduling configuration.
    # Presence (non-NULL) means "this task recurs every N seconds"; absence
    # means "never scheduled" -- the same meaning `schedule` above has
    # always had by virtue of being unread. SYNC-only in V1, enforced at
    # the Pydantic layer (app/schemas/task.py), not here -- this column has
    # no task_type-conditional constraint of its own.
    schedule_interval_seconds: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    # The next due instant, UTC. NULL exactly when schedule_interval_seconds
    # is NULL (see ck_tasks_schedule_consistency above). Advanced by
    # app.worker.scheduler.run_due_schedules() on every claim; recomputed
    # from "now" (never from the old value) whenever the interval is newly
    # set or changed via the API. Left stored-but-inert on soft-delete --
    # is_active=false already excludes the row from every scheduler query
    # and from the partial index above, so no explicit clearing is needed.
    next_run_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
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
