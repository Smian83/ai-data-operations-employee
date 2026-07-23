"""
CleaningRun: one row per Module 6 cleaning TaskRun -- immutable core fields
(what was cleaned, what changed, the output location) plus mutable
approval-state fields (status and who/when for each transition). Mirrors
DataProfile's one-row-per-run idempotency pattern (uq_cleaning_runs_task_
run_id). See docs/module-6-data-cleaning-engine-design.md Sections 11, 12,
15.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base

# Plain string, not a native Postgres enum -- matches TaskRunEvent.
# event_type's existing precedent in this codebase (a small, worker-owned
# state value) and avoids the migration/model DDL-ownership bug class this
# project has hit twice with native enums. See the design doc Section 15.
CLEANING_RUN_STATUSES = ("pending_review", "approved", "rejected", "rolled_back")


class CleaningRun(Base):
    __tablename__ = "cleaning_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_cleaning_runs_org_task_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_cleaning_runs_org_task",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_cleaning_runs_org_data_source",
            ondelete="RESTRICT",
        ),
        # The prior SYNC run whose DataProfile this cleaning run was
        # produced from -- denormalized from TaskRun.source_task_run_id so
        # callers can find it without an extra join.
        ForeignKeyConstraint(
            ["organization_id", "source_task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_cleaning_runs_org_source_task_run",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("task_run_id", name="uq_cleaning_runs_task_run_id"),
        # Required so CleaningChange can have a tenant-aware composite FK
        # (organization_id, cleaning_run_id) -> (organization_id, id), same
        # pattern as every other Module 3-5 parent table (e.g.
        # uq_task_runs_org_id) -- SQLite enforces that a composite FK's
        # target columns exactly match a UNIQUE constraint on the parent,
        # not merely a PK plus a separate index.
        UniqueConstraint("organization_id", "id", name="uq_cleaning_runs_org_id"),
        CheckConstraint(
            "status IN ('pending_review', 'approved', 'rejected', 'rolled_back')",
            name="ck_cleaning_runs_status_valid",
        ),
        CheckConstraint("row_count >= 0", name="ck_cleaning_runs_row_count_nonnegative"),
        CheckConstraint(
            "total_changes_count >= 0", name="ck_cleaning_runs_total_changes_nonnegative"
        ),
        CheckConstraint(
            "duplicate_row_count >= 0", name="ck_cleaning_runs_duplicate_count_nonnegative"
        ),
        CheckConstraint(
            "confidence_score >= 0 AND confidence_score <= 1",
            name="ck_cleaning_runs_confidence_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    data_source_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    source_task_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)

    output_file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    output_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    row_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    total_changes_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    changes_by_rule: Mapped[dict] = mapped_column(JSON, nullable=False)
    duplicate_row_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float(), nullable=False)

    post_clean_row_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    post_clean_missing_value_total: Mapped[int] = mapped_column(Integer(), nullable=False)
    post_clean_duplicate_row_count: Mapped[int] = mapped_column(Integer(), nullable=False)

    # Which app.cleaning.engine.CLEANING_ENGINE_VERSION produced this run.
    # Never changes after creation -- so a future rule-set change leaves
    # every existing CleaningRun fully traceable to the exact engine
    # version that actually produced it. See the design doc Section 15.
    cleaning_engine_version: Mapped[str] = mapped_column(String(20), nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending_review")
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rolled_back_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    rolled_back_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Module 13: NULL means the artifact at output_file_path still exists.
    # Non-NULL is the sole authoritative "this file no longer exists"
    # signal -- set only after app.worker.retention confirms a real
    # deletion or confirms the file was already absent. output_file_path/
    # output_sha256 above are never cleared on purge -- they remain the
    # historical record of what was produced and its hash, even after the
    # bytes themselves are gone. See
    # docs/module-13-output-artifact-retention-design.md.
    output_deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    changes: Mapped[list["CleaningChange"]] = relationship(  # noqa: F821
        back_populates="cleaning_run", order_by="CleaningChange.row_index"
    )

    def __repr__(self) -> str:
        return (
            f"CleaningRun(id={self.id!r}, task_run={self.task_run_id!r}, "
            f"status={self.status!r})"
        )
