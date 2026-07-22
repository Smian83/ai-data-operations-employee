"""
ArtifactDownloadEvent: one row per AUTHORIZED artifact-download attempt
against a CleaningRun/StandardizationRun/ExportRun output file (Module
10). Unauthorized, cross-organization, nonexistent-resource,
pending_review, and rejected-run requests create NO row here, since
authorization never succeeded for them -- there is nothing to audit
about a request that never reached the artifact. See
docs/module-10-artifact-retrieval-design.md Sections 6, 7, 10.

Lifecycle (explicit, not a single point-in-time record): exactly one row
is created, outcome='started', immediately after tenant-scoped lookup +
downloadable-state authorization + path containment + file-existence
validation all succeed. That same row is later updated EXACTLY ONCE, in
place, to its terminal outcome -- never a second insert for the same
authorized attempt.

Field mutability (corrected during architectural review from an earlier
draft that claimed every column was immutable from insertion): id,
organization_id, artifact_type, the three run-id columns, downloaded_by,
run_status_at_request, and created_at are immutable once written --
identity and request-context fields, fixed at creation. outcome,
failure_reason_code, verified_sha256, bytes_served, and completed_at are
mutable exactly once, by trusted server-side download-lifecycle code
only (no client-facing endpoint ever reads, filters, or writes this
table) -- the single transition from 'started' to a terminal outcome.
Once a row reaches a terminal outcome it is never updated again, the
same no-second-write-path enforcement every other append-only audit
table in this project (CleaningChange, StandardizationChange,
MatchDecision, ExportRowExclusion) already relies on.

No CLIENT_ABORTED outcome exists: whether this stack's FastAPI/Starlette
streaming-response lifecycle can reliably and transactionally
distinguish a client disconnect from a server-side I/O failure was not
established by inspection, so 'stream_failed' deliberately covers both
rather than asserting an unverified detection guarantee.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    String,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

# Controlled vocabularies -- plain tuples, not native Postgres enum types
# or enum.Enum classes, matching the STANDARDIZATION_RUN_STATUSES/
# MATCH_RUN_STATUSES/EXPORT_RUN_STATUSES precedent in app.models.enums
# (small, internal, server-owned value sets).
ARTIFACT_TYPES = ("cleaning", "standardization", "export")

# The run's approval status at the moment authorization succeeded for
# this attempt. pending_review/rejected runs never reach the point where
# a row is created (Section 11 of the design doc), so only these two
# values are ever valid here.
ARTIFACT_DOWNLOAD_RUN_STATUSES = ("approved", "rolled_back")

ARTIFACT_DOWNLOAD_OUTCOMES = (
    "started",
    "completed",
    "integrity_failed",
    "file_missing",
    "stream_failed",
)

# Controlled internal failure codes -- never raw exception text, never a
# filesystem path. Present exactly when outcome is a failure outcome.
ARTIFACT_DOWNLOAD_FAILURE_REASON_CODES = (
    "hash_mismatch",
    "file_not_found",
    "not_a_regular_file",
    "path_containment_violation",
    "io_error",
    "stream_interrupted",
)


class ArtifactDownloadEvent(Base):
    __tablename__ = "artifact_download_events"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "cleaning_run_id"],
            ["cleaning_runs.organization_id", "cleaning_runs.id"],
            name="fk_artifact_download_events_org_cleaning_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "standardization_run_id"],
            ["standardization_runs.organization_id", "standardization_runs.id"],
            name="fk_artifact_download_events_org_standardization_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "export_run_id"],
            ["export_runs.organization_id", "export_runs.id"],
            name="fk_artifact_download_events_org_export_run",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "artifact_type IN (" + ", ".join(f"'{t}'" for t in ARTIFACT_TYPES) + ")",
            name="ck_artifact_download_events_artifact_type_valid",
        ),
        # Exactly one of the three run-id columns is set -- which artifact
        # this event describes is never ambiguous.
        CheckConstraint(
            "(CASE WHEN cleaning_run_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN standardization_run_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN export_run_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_artifact_download_events_exactly_one_run_ref",
        ),
        CheckConstraint(
            "run_status_at_request IN ("
            + ", ".join(f"'{s}'" for s in ARTIFACT_DOWNLOAD_RUN_STATUSES)
            + ")",
            name="ck_artifact_download_events_run_status_valid",
        ),
        CheckConstraint(
            "outcome IN (" + ", ".join(f"'{o}'" for o in ARTIFACT_DOWNLOAD_OUTCOMES) + ")",
            name="ck_artifact_download_events_outcome_valid",
        ),
        # A failure_reason_code exists exactly when outcome is a failure
        # outcome -- never present for started/completed, always present
        # otherwise.
        CheckConstraint(
            "(outcome IN ('started', 'completed') AND failure_reason_code IS NULL) OR "
            "(outcome NOT IN ('started', 'completed') AND failure_reason_code IS NOT NULL)",
            name="ck_artifact_download_events_failure_reason_matches_outcome",
        ),
        CheckConstraint(
            "failure_reason_code IS NULL OR failure_reason_code IN ("
            + ", ".join(f"'{c}'" for c in ARTIFACT_DOWNLOAD_FAILURE_REASON_CODES)
            + ")",
            name="ck_artifact_download_events_failure_reason_code_valid",
        ),
        # completed_at is set exactly when outcome has left 'started'.
        CheckConstraint(
            "(outcome = 'started' AND completed_at IS NULL) OR "
            "(outcome != 'started' AND completed_at IS NOT NULL)",
            name="ck_artifact_download_events_completed_at_matches_outcome",
        ),
        CheckConstraint(
            "bytes_served >= 0", name="ck_artifact_download_events_bytes_served_nonnegative"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    artifact_type: Mapped[str] = mapped_column(String(20), nullable=False)

    # Exactly one of these three is non-null (enforced above).
    cleaning_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    standardization_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), nullable=True, index=True
    )
    export_run_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)

    # The user who initiated this download attempt. SET NULL on account
    # deletion, same convention as approved_by/rejected_by/rolled_back_by
    # across every run table.
    downloaded_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )

    # Captured at the moment authorization succeeded -- never revised
    # afterward, even if the underlying run's status later changes.
    run_status_at_request: Mapped[str] = mapped_column(String(20), nullable=False)

    outcome: Mapped[str] = mapped_column(String(20), nullable=False, default="started", index=True)
    failure_reason_code: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Set once pre-stream verification succeeds; NULL for
    # integrity_failed/file_missing (verification never succeeded); may
    # still be set under a later stream_failed (verification succeeded,
    # the subsequent transfer did not complete).
    verified_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bytes_served: Mapped[int] = mapped_column(BigInteger(), nullable=False, default=0)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"ArtifactDownloadEvent(id={self.id!r}, artifact_type={self.artifact_type!r}, "
            f"outcome={self.outcome!r})"
        )
