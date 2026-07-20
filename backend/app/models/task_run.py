"""
TaskRun: one execution record of a Task. Module 3 creates these only in the
'pending' state (an enqueue stub) — actually transitioning a run through
running -> success/failed is Module 4's execution engine. The CHECK
constraints below exist so the schema is correct *now*, since Module 4 will
likely write these rows directly (not necessarily through this API).
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Text,
    Uuid,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import TaskRunStatus

task_run_status_enum = SAEnum(
    TaskRunStatus,
    name="task_run_status_enum",
    values_callable=lambda obj: [e.value for e in obj],
    create_constraint=True,
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

    task: Mapped["Task"] = relationship(back_populates="runs")  # noqa: F821

    def __repr__(self) -> str:
        return f"TaskRun(id={self.id!r}, task={self.task_id!r}, status={self.status!r})"
