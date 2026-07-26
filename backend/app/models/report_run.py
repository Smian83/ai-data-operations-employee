"""
ReportRun: immutable pipeline summary report produced for one Module 20
REPORT TaskRun. Direct structural sibling of QualityControlRun (Module 18)
and other immutable-audit-row modules -- one row per TaskRun, no status
column, no approval fields, never updated after creation.

The report engine reads already-persisted run rows from every prior pipeline
stage (DataProfile, IssueDetectionRun, RemediationRun, ValidationRun,
QualityControlRun, CleanExport, etc.) and builds a single, structured JSON
report stored in the report_data column. No file artifact is written to disk.
The download endpoint serializes report_data to JSON bytes on the fly.

Chain resolution:
  The REPORT TaskRun's source_task_run_id should point to a QUALITY_CTRL
  TaskRun (which already stores all denormalized upstream IDs). If
  source_task_run_id is NULL or points to a non-QUALITY_CTRL run, the
  handler uses the latest QualityControlRun for the task's data_source.

report_data JSON schema version is embedded in the JSON itself under
report_schema_version (same forward-compatibility pattern as
QualityControlRun.execution_snapshot's snapshot_version key).

No processing_duration_ms column -- same decision as every prior run-summary
table. Derived at the API layer from TaskRun.started_at/finished_at.

Two UNIQUE constraints:
  uq_report_runs_task_run_id   -- idempotency: one row per REPORT TaskRun.
  uq_report_runs_org_id        -- required so future composite FK targets work.

quality_control_run_id is nullable: NULL for a REPORT task that ran when
no QualityControlRun existed yet (partial-pipeline report). Non-null for
a full-pipeline report anchored to a specific QC run.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    JSON,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ReportRun(Base):
    __tablename__ = "report_runs"
    __table_args__ = (
        # ---- Composite FK → task_runs (CASCADE) ----
        ForeignKeyConstraint(
            ["organization_id", "task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_report_runs_org_task_run",
            ondelete="CASCADE",
        ),
        # ---- Composite FK → tasks (RESTRICT) ----
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_report_runs_org_task",
            ondelete="RESTRICT",
        ),
        # ---- Composite FK → data_sources (RESTRICT) ----
        ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_report_runs_org_data_source",
            ondelete="RESTRICT",
        ),
        # ---- Idempotency: one ReportRun per REPORT TaskRun ----
        UniqueConstraint("task_run_id", name="uq_report_runs_task_run_id"),
        # ---- Required so future composite FK targets can reference (org, id) ----
        UniqueConstraint("organization_id", "id", name="uq_report_runs_org_id"),
        # NOTE: quality_control_run_id FK is PostgreSQL-only (composite FK
        # via op.create_foreign_key in migration). SQLite does not support
        # ALTER TABLE ADD CONSTRAINT, so application-layer referential integrity
        # (handler always writes valid IDs) is sufficient for tests. Same
        # precedent as validation_runs.applied_remediation_run_id.
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    data_source_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)

    # The QualityControlRun that anchored this report. Nullable: NULL when
    # the pipeline has not yet reached the QUALITY_CTRL stage (partial report).
    quality_control_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), nullable=True, index=True
    )

    # Report and schema version -- bumped independently when engine logic or
    # report_data schema changes. Both are embedded in report_data JSON as
    # well (report_engine_version, report_schema_version) for self-description.
    report_engine_version: Mapped[str] = mapped_column(String(20), nullable=False)
    report_schema_version: Mapped[str] = mapped_column(String(10), nullable=False)

    # The full structured report JSON -- schema described in the report engine
    # (app.reports.engine). Stored inline (same pattern as
    # QualityControlRun.execution_snapshot) -- no file artifact, no file root.
    # The download endpoint serializes this column to JSON bytes on the fly.
    report_data: Mapped[dict] = mapped_column(JSON, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"ReportRun(id={self.id!r}, "
            f"task_run={self.task_run_id!r}, "
            f"qc_run={self.quality_control_run_id!r})"
        )
