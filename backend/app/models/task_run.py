"""
TaskRun: one execution record of a Task.

Module 3 created these only in the 'pending' state (an enqueue stub). Module 4
adds the execution-bookkeeping columns needed for a safe, concurrent worker
pool to actually run them:

- lease_token: a fresh UUID generated on every claim (not a stable
  worker id). Heartbeats and completion must present the *current*
  lease_token, so a worker whose lease already expired and was reclaimed by
  someone else can never complete a run it no longer owns -- even if it
  wakes up and finishes the work late. This is a fencing token, not just an
  ownership label.
- lease_expires_at / last_heartbeat_at: the timeout/liveness mechanism the
  reaper uses to detect and recover stuck runs.
- attempt_count / next_retry_at: retry bookkeeping. A retry resets
  started_at/finished_at/error_message back to NULL (status returns to
  'pending'), so Module 3's existing CHECK constraints below needed NO
  changes at all -- only new, independently-nullable columns were added.
- idempotency_key: generated once, at row creation, and never changed again
  (including across retries of the same row). Handlers must pass this value
  to any downstream system whose write they perform, so that a duplicate
  execution of the same TaskRun (e.g. a retry after a handler crashed after
  its side effect but before reporting success) cannot create a duplicate
  downstream effect. It intentionally is NOT the same as `id`: `id` is a
  row identity, `idempotency_key` is an execution identity contract handed
  to external systems.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
# NOTE: this must be sqlalchemy.dialects.postgresql.ENUM, NOT the generic
# sqlalchemy.Enum. The generic Enum silently ignores an unrecognized
# create_type kwarg (its __init__ never pops it), so create_type=False
# below would otherwise have no effect and Base.metadata.create_all()
# would independently emit CREATE TYPE against a live PostgreSQL database
# -- exactly the DuplicateObject bug hit during Module 4 verification.
# Only postgresql.ENUM genuinely implements and honors create_type.
from sqlalchemy.dialects.postgresql import ENUM as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import TaskRunStatus

# create_type=False: see the identical comment on source_type_enum in
# app/models/data_source.py -- Alembic migrations own CREATE/DROP TYPE for
# every native PostgreSQL enum in this project; models must never issue it
# independently via Base.metadata.create_all().
task_run_status_enum = SAEnum(
    TaskRunStatus,
    name="task_run_status_enum",
    values_callable=lambda obj: [e.value for e in obj],
    create_constraint=True,
    create_type=False,
)


class TaskRun(Base):
    __tablename__ = "task_runs"
    __table_args__ = (
        # Tenant-aware composite FK: a TaskRun's organization_id must match
        # its Task's organization_id, enforced by PostgreSQL.
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_task_runs_org_task",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "(status = 'pending' AND started_at IS NULL AND finished_at IS NULL "
            "  AND error_message IS NULL)"
            " OR (status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL)"
            " OR (status = 'success' AND started_at IS NOT NULL AND finished_at IS NOT NULL"
            "     AND error_message IS NULL)"
            " OR (status = 'failed' AND started_at IS NOT NULL AND finished_at IS NOT NULL"
            "     AND error_message IS NOT NULL)",
            name="ck_task_runs_status_invariants",
        ),
        CheckConstraint(
            "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
            name="ck_task_runs_finished_after_started",
        ),
        # Module 4: a running/claimed row must always carry a lease_token and
        # a lease_expires_at (both set together on claim, both cleared
        # together on any terminal/requeue transition). Prevents a
        # half-claimed row (token without expiry, or vice versa) from ever
        # being persisted.
        CheckConstraint(
            "(status = 'running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)"
            " OR (status != 'running' AND lease_token IS NULL AND lease_expires_at IS NULL)",
            name="ck_task_runs_lease_consistency",
        ),
        # Required so TaskRunEvent can have a tenant-aware composite FK
        # (organization_id, task_run_id) -> (organization_id, id), same
        # pattern as every other Module 3/4 parent table.
        UniqueConstraint("organization_id", "id", name="uq_task_runs_org_id"),
        UniqueConstraint("idempotency_key", name="uq_task_runs_idempotency_key"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    # Denormalized (also derivable via task_id -> tasks.organization_id) so
    # that tenant-scoped queries on this — the highest-volume table — never
    # depend on remembering to join through Task. Also what makes the
    # composite tenant-aware FK to `tasks` possible.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    status: Mapped[TaskRunStatus] = mapped_column(
        task_run_status_enum, nullable=False, default=TaskRunStatus.PENDING
    )
    triggered_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    log_output: Mapped[str | None] = mapped_column(Text(), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # --- Module 4: execution engine bookkeeping (all additive/nullable) ---
    idempotency_key: Mapped[uuid.UUID] = mapped_column(
        Uuid(), nullable=False, default=uuid.uuid4
    )
    lease_token: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)
    next_retry_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    task: Mapped["Task"] = relationship(back_populates="runs")  # noqa: F821
    events: Mapped[list["TaskRunEvent"]] = relationship(  # noqa: F821
        back_populates="task_run", order_by="TaskRunEvent.created_at"
    )

    def __repr__(self) -> str:
        return f"TaskRun(id={self.id!r}, task={self.task_id!r}, status={self.status!r})"
