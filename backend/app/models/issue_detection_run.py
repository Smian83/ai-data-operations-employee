"""
IssueDetectionRun: immutable summary produced for one Module 14 DETECT
TaskRun. Direct structural sibling of DataProfile (Module 5) -- one row per
TaskRun, no status column, no approval fields, never updated after
creation. Unlike CleaningRun/StandardizationRun/ExportRun/MatchRun, there
is nothing here for a human to approve, reject, or roll back: the engine
never mutates source data, so there is no output to gate.

The engine analyzes the raw synced source file directly (the same
tenant-scoped CSV_INPUT_ROOT file app.profiling.csv_loader already reads
for Module 5), independent of and prior to any Module 6/7 cleaning or
standardization -- see app.worker.handlers.issue_detection.

total_issues_found / issues_by_severity / issues_by_type are always the
true, uncapped counts, computed once over every finding the engine
produced during the pass -- never derived from however many Issue rows
were actually persisted. Individual Issue rows are capped per run at
settings.issue_detection_max_persisted_issues (see
app.worker.handlers.issue_detection), the same bounded-but-never-silent
pattern CleaningRun.total_changes_count/CleaningChange already established
in Module 6: the summary on this row is always accurate even when the
detail rows beneath it are capped.

Module 15 addition: source_sha256 (nullable). Added additively so Module
15's RemediationHandler can verify it is remediating the exact same
dataset version this run scanned -- app.profiling.csv_loader.load_csv
already computes this hash for every file it loads (LoadedCsv.
source_sha256; DataProfile has stored the equivalent since Module 5), it
was simply never threaded through to this table before. Existing rows
predate this column and are NULL; app.worker.handlers.remediation treats a
NULL upstream hash as a permanent failure ("re-run detection before
remediation"), never as a skipped or passed check. See
docs/module-15-deterministic-cleaning-engine-design.md Section 2.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
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


class IssueDetectionRun(Base):
    __tablename__ = "issue_detection_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_issue_detection_runs_org_task_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_issue_detection_runs_org_task",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_issue_detection_runs_org_data_source",
            ondelete="RESTRICT",
        ),
        # Required so Issue can have a composite FK (organization_id,
        # detection_run_id) -> (organization_id, id), same pattern as every
        # other Module 6/7/8/9/13 parent-of-child-audit-rows table.
        UniqueConstraint("organization_id", "id", name="uq_issue_detection_runs_org_id"),
        UniqueConstraint("task_run_id", name="uq_issue_detection_runs_task_run_id"),
        CheckConstraint("rows_scanned >= 0", name="ck_issue_detection_runs_rows_scanned_nonneg"),
        CheckConstraint(
            "columns_scanned >= 0", name="ck_issue_detection_runs_columns_scanned_nonneg"
        ),
        CheckConstraint(
            "total_issues_found >= 0", name="ck_issue_detection_runs_total_issues_nonneg"
        ),
        CheckConstraint(
            "persisted_issue_count >= 0 AND persisted_issue_count <= total_issues_found",
            name="ck_issue_detection_runs_persisted_count_valid",
        ),
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

    rows_scanned: Mapped[int] = mapped_column(Integer(), nullable=False)
    columns_scanned: Mapped[int] = mapped_column(Integer(), nullable=False)

    # True, uncapped totals -- see module docstring above.
    total_issues_found: Mapped[int] = mapped_column(Integer(), nullable=False)
    # How many of total_issues_found actually got an Issue row (see
    # settings.issue_detection_max_persisted_issues). Equal to
    # total_issues_found whenever the cap was not reached.
    persisted_issue_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    # {severity: count}, every key in ISSUE_SEVERITIES present (0 if none
    # found), true totals -- see app.models.enums.ISSUE_SEVERITIES.
    issues_by_severity: Mapped[dict] = mapped_column(JSON, nullable=False)
    # {issue_type: count}, only keys with at least one finding present,
    # true totals -- see app.models.enums.ISSUE_TYPES.
    issues_by_type: Mapped[dict] = mapped_column(JSON, nullable=False)
    # What was actually in effect for this pass (persisted-issue cap,
    # outlier z-score threshold, etc.) -- same transparency purpose as
    # DataProfile.limits_applied.
    limits_applied: Mapped[dict] = mapped_column(JSON, nullable=False)

    detection_engine_version: Mapped[str] = mapped_column(String(20), nullable=False)
    detected_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Module 15 addition (nullable): the source file's SHA-256 at the time
    # this run scanned it, so app.worker.handlers.remediation can verify
    # it is remediating the identical dataset version, not just the same
    # data_source_id. NULL on every row created before this column existed
    # -- see module docstring above.
    source_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)

    task_run: Mapped["TaskRun"] = relationship(back_populates="issue_detection_run")  # noqa: F821
    issues: Mapped[list["Issue"]] = relationship(  # noqa: F821
        back_populates="detection_run", order_by="Issue.row_number"
    )

    def __repr__(self) -> str:
        return (
            f"IssueDetectionRun(id={self.id!r}, task_run={self.task_run_id!r}, "
            f"total_issues_found={self.total_issues_found!r})"
        )
