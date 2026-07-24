"""
ArtifactRetentionEvent: one row per retention-pass evaluation of a
CleaningRun/StandardizationRun/ExportRun output artifact (Module 13).
Structural mirror of ArtifactDownloadEvent (Module 10) -- same exactly-
one-run-reference shape, same started-then-exactly-once-terminal outcome
lifecycle -- adapted for a worker-initiated background process rather
than a client-initiated request (no downloaded_by-equivalent column here:
no transition in this lifecycle is triggered by a human action; the human
decision already happened earlier, when the run itself was approved/
rejected/rolled_back through the existing per-run endpoints).

Lifecycle mapping (see docs/module-13-output-artifact-retention-design.md):
this table only ever represents the PURGE_PENDING state and its terminal
resolution. ACTIVE and EXPIRED (the two states before an artifact is ever
claimed) produce no row here at all -- they are derived, at query time,
from the owning run's own status/decision-timestamp columns, never
persisted.

Unlike ArtifactDownloadEvent's request/response-spanning started-then-
finalized flow, a retention row is written exactly ONCE per candidate,
already at its terminal outcome (completed/already_missing/failed) --
app.worker.retention claims the row (the row lock, held for that one
artifact's transaction, IS the PURGE_PENDING state; see that module's
docstring), performs the deletion attempt, and inserts this event already
resolved, all inside that same transaction, which then commits or rolls
back atomically. There is no separate earlier write at outcome='started'
to later update: a file deletion has no request/response boundary to span
the way a streamed HTTP download does, so the two-phase pattern
ArtifactDownloadEvent needs is unnecessary here. 'started' remains a
valid, schema-level outcome value (for forward compatibility with any
future two-phase caller) but is not produced by app.worker.retention today.

    outcome='completed'        -> owning run's output_deleted_at is set
                                   (real pass) or left NULL (dry run, see
                                   dry_run below); artifact reached PURGED
                                   (real pass only)
    outcome='already_missing'  -> owning run's output_deleted_at is set
                                   (real pass) or left NULL (dry run); the
                                   file was already gone -- an expected
                                   convergence outcome, not a failure
    outcome='failed'           -> owning run's output_deleted_at is left
                                   NULL; the artifact bounces back to
                                   EXPIRED and remains a candidate for the
                                   next pass

Because the claim, the deletion attempt, and the terminal outcome are all
decided and persisted in one database transaction, a worker crash
mid-operation rolls back the whole transaction -- no row is ever left
orphaned at an intermediate state the way ArtifactDownloadEvent's own
now-fixed fstat() bug (Module 10) once left a row stuck at 'started'.

dry_run rows never reach 'completed' with a real deletion having
happened -- see app.worker.retention for the exact mechanics. A dry-run
'completed' row still records the artifact's observed size
(artifact_size_bytes) for "would reclaim" reporting, without the file
ever actually being removed and without output_deleted_at ever being set
on the owning run.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Controlled vocabulary -- plain strings, matching ArtifactDownloadEvent's
# ARTIFACT_DOWNLOAD_OUTCOMES precedent (a small, internal, server-owned
# value set), not a native Postgres enum.
ARTIFACT_RETENTION_OUTCOMES = ("started", "completed", "already_missing", "failed")

# 'already_missing' is deliberately NOT a failure outcome here (contrast
# with ArtifactDownloadEvent, where file_missing IS a failure outcome) --
# an artifact that was already gone still reaches the PURGED end state
# successfully; nothing went wrong.
#
# Small, controlled vocabulary (never a raw exception message, never a
# filesystem path -- see app.worker.retention._classify_os_error and its
# module docstring): unsafe_path (resolve_artifact_path rejected the
# stored output path -- defense-in-depth only, since the path is always
# server-written, never client-supplied); permission_denied (an OSError
# whose PermissionError subtype was raised by the storage layer);
# invalid_artifact_type (defensive-only -- unreachable through the fixed,
# hand-written run-type configuration in app.worker.retention under
# normal operation); filesystem_error (any other OSError from the
# storage layer -- disk-full, I/O error, etc.); database_conflict
# (defensive-only -- reserved for a future caller that wants to record a
# lost concurrency race as a failure row rather than silently retrying
# it, which is what app.worker.retention itself does today; see that
# module's own guarded-UPDATE handling).
ARTIFACT_RETENTION_FAILURE_REASON_CODES = (
    "unsafe_path",
    "permission_denied",
    "invalid_artifact_type",
    "filesystem_error",
    "database_conflict",
)

# Independent of app.core.config.Settings.output_retention_window_days
# (the real, operator-facing, configurable value enforced in Pydantic on
# every retention-pass invocation) -- this is the fixed database safety
# floor beneath it, deliberately hand-kept-in-sync with
# RETENTION_WINDOW_HARD_FLOOR_DAYS in app.core.config, exactly the same
# "two constants, not one derived source of truth" relationship
# SCHEDULE_INTERVAL_HARD_FLOOR_SECONDS already established for Module 12.
RETENTION_WINDOW_HARD_FLOOR_DAYS_DB = 7


class ArtifactRetentionEvent(Base):
    __tablename__ = "artifact_retention_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "cleaning_run_id"],
            ["cleaning_runs.organization_id", "cleaning_runs.id"],
            name="fk_artifact_retention_events_org_cleaning_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "standardization_run_id"],
            ["standardization_runs.organization_id", "standardization_runs.id"],
            name="fk_artifact_retention_events_org_stdz_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "export_run_id"],
            ["export_runs.organization_id", "export_runs.id"],
            name="fk_artifact_retention_events_org_export_run",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "(CASE WHEN cleaning_run_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN standardization_run_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN export_run_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_artifact_retention_events_exactly_one_run_ref",
        ),
        CheckConstraint(
            "outcome IN (" + ", ".join(f"'{o}'" for o in ARTIFACT_RETENTION_OUTCOMES) + ")",
            name="ck_artifact_retention_events_outcome_valid",
        ),
        CheckConstraint(
            "(outcome IN ('started', 'completed', 'already_missing') "
            "AND failure_reason_code IS NULL) OR "
            "(outcome = 'failed' AND failure_reason_code IS NOT NULL)",
            name="ck_artifact_retention_events_failure_reason_matches_outcome",
        ),
        CheckConstraint(
            "failure_reason_code IS NULL OR failure_reason_code IN ("
            + ", ".join(f"'{c}'" for c in ARTIFACT_RETENTION_FAILURE_REASON_CODES)
            + ")",
            name="ck_artifact_retention_events_failure_reason_code_valid",
        ),
        CheckConstraint(
            "(outcome = 'started' AND completed_at IS NULL) OR "
            "(outcome != 'started' AND completed_at IS NOT NULL)",
            name="ck_artifact_retention_events_completed_at_matches_outcome",
        ),
        CheckConstraint(
            "retention_window_days_applied >= 0",
            name="ck_artifact_retention_events_window_days_nonnegative",
        ),
        CheckConstraint(
            f"retention_window_days_applied >= {RETENTION_WINDOW_HARD_FLOOR_DAYS_DB}",
            name="ck_artifact_retention_events_window_days_hard_floor",
        ),
        CheckConstraint(
            "artifact_size_bytes IS NULL OR artifact_size_bytes >= 0",
            name="ck_artifact_retention_events_artifact_size_bytes_nonneg",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Exactly one of these three is non-null (enforced above).
    cleaning_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    standardization_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), nullable=True, index=True
    )
    export_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)

    outcome: Mapped[str] = mapped_column(
        String(20), nullable=False, default="started", index=True
    )
    failure_reason_code: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # True for a pass run under OUTPUT_RETENTION_DRY_RUN=true -- a dry-run
    # row may still reach outcome='completed' (the artifact WOULD have
    # been purged), but the owning run's output_deleted_at is never set
    # and the file is never actually removed. See app.worker.retention.
    dry_run: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)

    # The exact retention window in effect for this evaluation, persisted
    # even if the operator's configured window changes later -- so every
    # past purge decision remains explainable against the policy that
    # actually produced it, not the policy in effect today.
    retention_window_days_applied: Mapped[int] = mapped_column(Integer(), nullable=False)

    # The artifact's observed size at evaluation time, when known -- set
    # for 'completed' (real or dry-run), NULL for 'already_missing' (no
    # file to measure) and NULL for 'failed' outcomes that occurred before
    # the file could be stat'd. Summing this column where dry_run=false
    # AND outcome='completed' is exactly the "storage reclaimed" metric;
    # summing it where dry_run=true AND outcome='completed' is exactly the
    # "storage that would be reclaimed" dry-run metric.
    artifact_size_bytes: Mapped[int | None] = mapped_column(BigInteger(), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    # Set exactly when outcome leaves 'started' -- same convention as
    # ArtifactDownloadEvent.completed_at.
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"ArtifactRetentionEvent(id={self.id!r}, outcome={self.outcome!r}, "
            f"dry_run={self.dry_run!r})"
        )
