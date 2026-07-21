"""
ExportRun: one row per Module 9 EXPORT TaskRun -- immutable core fields
(what was exported, the output location, self-describing file metadata)
plus mutable approval-state fields. Direct structural mirror of
StandardizationRun's one-row-per-run idempotency/approval-state shape
(see models/standardization_run.py), extended with a composite FK
straight to MatchRun (the approved Module 8 result this export
materializes) and four file-self-description fields added per
architectural review: output_file_size_bytes, output_column_count,
export_timestamp, csv_format_version. See
docs/module-9-data-export-engine-design.md Sections 3, 7, 8.

export_timestamp is DATABASE METADATA ONLY -- it is never written into
the exported CSV file itself (clarified during architectural review), so
that two independent EXPORT TaskRuns against identical approved input
still produce byte-identical output files (same output_sha256, same
output_file_size_bytes), differing only in this one field.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import EXPORT_RUN_STATUSES


class ExportRun(Base):
    __tablename__ = "export_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_export_runs_org_task_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_export_runs_org_task",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_export_runs_org_data_source",
            ondelete="RESTRICT",
        ),
        # The MATCH run whose approved MatchRun this export was produced
        # from -- denormalized from TaskRun.source_task_run_id, same
        # rationale as MatchRun.source_task_run_id.
        ForeignKeyConstraint(
            ["organization_id", "source_task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_export_runs_org_source_task_run",
            ondelete="RESTRICT",
        ),
        # Denormalized copy of the approved MatchRun's id, so callers can
        # find it without re-deriving it via source_task_run_id.
        ForeignKeyConstraint(
            ["organization_id", "match_run_id"],
            ["match_runs.organization_id", "match_runs.id"],
            name="fk_export_runs_org_match_run",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("task_run_id", name="uq_export_runs_task_run_id"),
        # Required so ExportRowExclusion can have a tenant-aware composite
        # FK (organization_id, export_run_id) -> (organization_id, id),
        # the Module 6 lesson, applied from the start every module since.
        UniqueConstraint("organization_id", "id", name="uq_export_runs_org_id"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in EXPORT_RUN_STATUSES) + ")",
            name="ck_export_runs_status_valid",
        ),
        CheckConstraint(
            "source_row_count >= 0", name="ck_export_runs_source_row_count_nonnegative"
        ),
        CheckConstraint("row_count >= 0", name="ck_export_runs_row_count_nonnegative"),
        CheckConstraint(
            "row_count <= source_row_count", name="ck_export_runs_row_count_le_source"
        ),
        CheckConstraint(
            "excluded_row_count >= 0", name="ck_export_runs_excluded_row_count_nonnegative"
        ),
        CheckConstraint(
            "excluded_row_count = source_row_count - row_count",
            name="ck_export_runs_excluded_row_count_consistent",
        ),
        CheckConstraint(
            "duplicate_groups_materialized_count >= 0",
            name="ck_export_runs_duplicate_groups_materialized_nonnegative",
        ),
        CheckConstraint(
            "output_file_size_bytes >= 0", name="ck_export_runs_output_file_size_nonnegative"
        ),
        CheckConstraint(
            "output_column_count >= 1", name="ck_export_runs_output_column_count_min"
        ),
        CheckConstraint(
            "csv_format_version >= 1", name="ck_export_runs_csv_format_version_min"
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
    match_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)

    output_file_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    output_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    source_row_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    # True total even when persisted ExportRowExclusion rows are capped --
    # same bounded-but-never-silent pattern as MatchRun.duplicate_pairs_
    # count/ambiguous_pairs_count.
    excluded_row_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    duplicate_groups_materialized_count: Mapped[int] = mapped_column(Integer(), nullable=False)

    # --- File self-description (added per architectural review) --------
    # Lets the artifact's expected structure (size, column count, version,
    # generation time) be inspected without parsing the CSV. Actual
    # content-integrity verification against output_sha256 still requires
    # reading the file bytes -- this metadata describes the artifact, it
    # does not substitute for reading it.
    output_file_size_bytes: Mapped[int] = mapped_column(BigInteger(), nullable=False)
    # Includes the two reserved provenance columns:
    # len(standardized_columns) + 2, always -- see app.export.engine.
    output_column_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    # DATABASE METADATA ONLY -- never serialized into the CSV file itself.
    # The one field expected to differ between two independent EXPORT
    # TaskRuns over otherwise-identical input.
    export_timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    # Fixed deterministic constant for Module 9 (EXPORT_CSV_FORMAT_VERSION
    # in app.export.engine), starts at 1. Persisted per-run so old export
    # files remain self-describing even if a future module changes the
    # export column schema.
    csv_format_version: Mapped[int] = mapped_column(Integer(), nullable=False)

    export_engine_version: Mapped[str] = mapped_column(String(20), nullable=False)

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

    exclusions: Mapped[list["ExportRowExclusion"]] = relationship(  # noqa: F821
        back_populates="export_run", order_by="ExportRowExclusion.row_index"
    )

    def __repr__(self) -> str:
        return (
            f"ExportRun(id={self.id!r}, task_run={self.task_run_id!r}, "
            f"status={self.status!r})"
        )
