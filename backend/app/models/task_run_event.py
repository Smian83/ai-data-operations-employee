"""
TaskRunEvent: an append-only audit trail of every execution-engine
transition a TaskRun goes through (claimed, heartbeat, succeeded, failed,
requeued, reaped). Rows here are never updated or deleted -- the mutable
TaskRun row only ever reflects the *latest* attempt, but the full history of
every attempt (including full, untruncated error detail) survives here
independent of what the row currently shows.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, JSON, String, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class TaskRunEvent(Base):
    __tablename__ = "task_run_events"
    __table_args__ = (
        # Tenant-aware composite FK, same pattern as every other Module 3/4
        # table: an event's organization_id must match its TaskRun's.
        ForeignKeyConstraint(
            ["organization_id", "task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_task_run_events_org_task_run",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    # Denormalized for the same reason as TaskRun.organization_id: tenant-
    # scoped queries and the composite FK above both depend on it.
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    # e.g. "claimed", "heartbeat", "succeeded", "failed", "requeued", "reaped"
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    from_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str | None] = mapped_column(String(20), nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # Full, untruncated context: error message, traceback, retry reasoning,
    # etc. Never displayed on any public TaskRun response -- this table is
    # for operational/debugging visibility only.
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    task_run: Mapped["TaskRun"] = relationship(back_populates="events")  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"TaskRunEvent(task_run={self.task_run_id!r}, "
            f"event_type={self.event_type!r}, to_status={self.to_status!r})"
        )
