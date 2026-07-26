"""
CleanExport: one row per clean export request -- the immutable record of
a verified, artifact-producing export of an approved, quality-controlled
dataset. Module 19.

Only produced when ALL of the following hold for the target dataset:
  1. A Module 9 ExportRun with status='approved' exists for the task.
  2. A Module 18 QualityControlRun with release_recommendation IN
     ('PASS', 'PASS_WITH_WARNINGS') exists for the same data_source.
  3. The idempotency_key has not already been used for a completed export.

If any check fails, a CleanExport row is still written but with
status='blocked' (eligibility failure) or status='failed' (write/I-O
failure). The artifact_path, checksum, row_count, and column_count
columns are NULL for non-completed rows.

status lifecycle:
  pending    → created but not yet processed (reserved for async path)
  processing → actively writing the artifact
  completed  → artifact written, checksum stored, download available
  failed     → export attempt failed (failure_reason set)
  blocked    → dataset ineligible (failure_reason describes which check)
  expired    → artifact deleted by retention; metadata row remains

idempotency_key UNIQUE constraint:
  Callers supply a stable idempotency_key (a UUID v4 they generate).
  A second POST with the same key returns the existing row without
  re-running the export. Callers MUST NOT reuse keys across different
  format requests for the same dataset.

artifact_id:
  A server-generated UUID used to construct the artifact file path:
    {clean_export_output_root}/{organization_id}/{artifact_id}.{format}
  NULL for non-completed rows. The download endpoint resolves the path
  from (organization_id, artifact_id, format) at request time.

dataset_version:
  The QualityControlRun.id whose PASS/PASS_WITH_WARNINGS recommendation
  authorized this export. Denormalized here so the export record is
  self-describing without re-querying the QC chain. Nullable on blocked
  rows (where the QC check itself failed and no run exists or passed).

No approval state machine: clean exports are immutable once completed.
A new export with a fresh idempotency_key must be requested to re-export.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import CLEAN_EXPORT_FORMATS, CLEAN_EXPORT_STATUSES


class CleanExport(Base):
    __tablename__ = "clean_exports"
    __table_args__ = (
        # ---- Composite FK → tasks (RESTRICT) ----
        # job_id = task_id in this project's terminology; RESTRICT prevents
        # the task from being deleted while a clean export record references it.
        ForeignKeyConstraint(
            ["organization_id", "job_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_clean_exports_org_job",
            ondelete="RESTRICT",
        ),
        # ---- Composite FK → data_sources (RESTRICT) ----
        ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_clean_exports_org_data_source",
            ondelete="RESTRICT",
        ),
        # ---- Idempotency: one clean export per caller-supplied key ----
        UniqueConstraint("idempotency_key", name="uq_clean_exports_idempotency_key"),
        # ---- Required so future composite FK targets work ----
        UniqueConstraint(
            "organization_id", "id", name="uq_clean_exports_org_id"
        ),
        # ---- status closed vocabulary ----
        CheckConstraint(
            "status IN ("
            + ", ".join(f"'{s}'" for s in CLEAN_EXPORT_STATUSES)
            + ")",
            name="ck_clean_exports_status_valid",
        ),
        # ---- format closed vocabulary ----
        CheckConstraint(
            "format IN ("
            + ", ".join(f"'{f}'" for f in CLEAN_EXPORT_FORMATS)
            + ")",
            name="ck_clean_exports_format_valid",
        ),
        # ---- row_count and column_count non-negative when non-null ----
        CheckConstraint(
            "row_count IS NULL OR row_count >= 0",
            name="ck_clean_exports_row_count_nonneg",
        ),
        CheckConstraint(
            "column_count IS NULL OR column_count >= 1",
            name="ck_clean_exports_column_count_min",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # job_id = task_id in this project (Tasks ARE jobs). References the
    # Module 9 EXPORT Task whose approved ExportRun backs this clean export.
    job_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), nullable=False, index=True
    )

    # The QualityControlRun.id that authorized this export (PASS or
    # PASS_WITH_WARNINGS recommendation). NULL for blocked rows where the
    # QC check itself failed (no passing QC run exists).
    dataset_version: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), nullable=True, index=True
    )

    # 'csv' | 'xlsx' -- enforced by ck_clean_exports_format_valid.
    format: Mapped[str] = mapped_column(String(10), nullable=False)

    # Status lifecycle (see module docstring).
    # 'completed' | 'failed' | 'blocked' | 'pending' | 'processing' | 'expired'
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    # Server-generated UUID used to construct the artifact file path
    # {clean_export_output_root}/{organization_id}/{artifact_id}.{format}.
    # NULL for non-completed rows.
    artifact_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)

    # SHA-256 hex digest of the artifact file bytes. NULL for non-completed rows.
    checksum: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Row and column counts of the exported data. NULL for non-completed rows.
    row_count: Mapped[int | None] = mapped_column(Integer(), nullable=True)
    column_count: Mapped[int | None] = mapped_column(Integer(), nullable=True)

    # Caller-supplied idempotency key. UNIQUE enforced by
    # uq_clean_exports_idempotency_key. Callers must not reuse keys.
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)

    # Human-readable explanation for blocked/failed status. NULL for
    # completed rows.
    failure_reason: Mapped[str | None] = mapped_column(Text(), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Set to now() when status transitions to 'completed', 'failed', or
    # 'blocked'. NULL for pending/processing rows.
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    def __repr__(self) -> str:
        return (
            f"CleanExport(id={self.id!r}, "
            f"job_id={self.job_id!r}, "
            f"format={self.format!r}, "
            f"status={self.status!r})"
        )
