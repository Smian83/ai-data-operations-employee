"""
StandardizationRun: one row per Module 7 standardization TaskRun --
immutable core fields (what was standardized, what changed, the output
location) plus mutable approval-state fields (status and who/when for
each transition). Direct structural mirror of CleaningRun (see
models/cleaning_run.py) -- same one-row-per-run idempotency pattern via
uq_standardization_runs_task_run_id. See
docs/module-7-data-standardization-engine-design.md Sections 3, 7, 8.
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
from app.models.enums import STANDARDIZATION_RUN_STATUSES


class StandardizationRun(Base):
    __tablename__ = "standardization_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_standardization_runs_org_task_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_standardization_runs_org_task",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_standardization_runs_org_data_source",
            ondelete="RESTRICT",
        ),
        # The TRANSFORM run whose approved CleaningRun this standardization
        # run was produced from -- denormalized from TaskRun.source_task_
        # run_id so callers can find it without an extra join, same
        # rationale as CleaningRun.source_task_run_id.
        ForeignKeyConstraint(
            ["organization_id", "source_task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_standardization_runs_org_source_task_run",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("task_run_id", name="uq_standardization_runs_task_run_id"),
        # Required so StandardizationChange can have a tenant-aware
        # composite FK (organization_id, standardization_run_id) ->
        # (organization_id, id) -- the exact constraint Module 6's
        # CleaningRun initially shipped without and had to add after
        # integration testing surfaced the SQLite FK-mismatch failure.
        # Added here from the start.
        UniqueConstraint("organization_id", "id", name="uq_standardization_runs_org_id"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in STANDARDIZATION_RUN_STATUSES) + ")",
            name="ck_standardization_runs_status_valid",
        ),
        CheckConstraint("row_count >= 0", name="ck_standardization_runs_row_count_nonnegative"),
        CheckConstraint(
            "total_changes_count >= 0", name="ck_standardization_runs_total_changes_nonnegative"
        ),
        CheckConstraint(
            "confidence_score >= 0 AND confidence_score <= 1",
            name="ck_standardization_runs_confidence_range",
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
    confidence_score: Mapped[float] = mapped_column(Float(), nullable=False)

    # Which app.standardization.engine.STANDARDIZATION_ENGINE_VERSION
    # produced this run. Never changes after creation. See the design
    # doc Section 3/7 -- individual StandardizationChange rows also carry
    # their own rule_version for finer-grained traceability than this.
    standardization_engine_version: Mapped[str] = mapped_column(String(20), nullable=False)

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

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    changes: Mapped[list["StandardizationChange"]] = relationship(  # noqa: F821
        back_populates="standardization_run", order_by="StandardizationChange.row_index"
    )

    def __repr__(self) -> str:
        return (
            f"StandardizationRun(id={self.id!r}, task_run={self.task_run_id!r}, "
            f"status={self.status!r})"
        )

