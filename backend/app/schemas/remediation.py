"""Read-only API schemas for Module 15 Phase 4 -- the two DTOs
GET /tasks/{task_id}/runs/{run_id}/remediation and
GET /tasks/{task_id}/runs/{run_id}/remediation/changes return. Same shape
convention as app.schemas.issue_detection_run / app.schemas.issue: the
summary DTO (RemediationRunRead) is cheap because every count on it was
already computed by app.worker.handlers.remediation at persist time --
this schema performs no aggregation of its own except the two fields
documented below, both trivial reads-plus-one-join, never a re-run of the
pure engine or a full table scan.

dataset_sha256 and processing_duration_ms are NOT columns on
RemediationRun -- per the approved Phase 4 instruction not to duplicate
data the shared TaskRun model (and, for the hash, the upstream
IssueDetectionRun) already records:
  - dataset_sha256 is read from the upstream IssueDetectionRun.
    source_sha256 (the exact hash RemediationHandler verified the source
    file against before proposing anything -- see that handler's own
    docstring), joined via RemediationRun.source_task_run_id. Never
    re-hashed here.
  - processing_duration_ms is computed from the owning TaskRun's own
    started_at/finished_at (both already recorded by app.worker.engine
    for every TaskRun, regardless of task_type) -- (finished_at -
    started_at) in whole milliseconds, or None if the run has not
    finished yet (finished_at IS NULL, e.g. mid-execution or, in
    practice, never observable through this endpoint since the
    RemediationRun row itself is only ever written after the handler,
    and therefore the TaskRun, completes).

changes_by_column is also NOT a column on RemediationRun -- it is a cheap
GROUP BY aggregation over RemediationChange.column_name for this run,
computed once per request by app.api.tasks.get_task_run_remediation (same
"a small aggregate query is still read-only orchestration, not writing
anything" reasoning Module 11's review-queue UNION ALL aggregation
already established). Whole-row actions (remove_duplicate_row /
remove_duplicate_primary_key) have no column_name and are excluded from
this breakdown, same as they are naturally absent from any per-column
view."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class RemediationRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    task_run_id: uuid.UUID
    task_id: uuid.UUID
    data_source_id: uuid.UUID
    source_task_run_id: uuid.UUID

    issues_considered_count: int
    total_changes_count: int
    issues_skipped_count: int
    changes_by_action: dict[str, int]
    changes_by_column: dict[str, int]
    skipped_by_reason: dict[str, int]

    remediation_engine_version: str
    dataset_sha256: str | None
    processing_duration_ms: int | None

    # Module 16 Phase 3: decision breakdown for this run.
    # Keys are always present: "pending", "approved", "rejected".
    # pending = total_changes_count - approved - rejected.
    # Computed by a single GROUP BY query in the endpoint; never stored on
    # RemediationRun (same "cheap read-only aggregation" reasoning as
    # changes_by_column above).
    decision_summary: dict[str, int]

    created_at: datetime


class RemediationChangeRead(BaseModel):
    """Plain BaseModel (not from_attributes=True), same reasoning as
    IssueRead: every field is set explicitly by the endpoint rather than
    built directly off the ORM row, so "no SQLAlchemy model is ever
    returned directly" holds here too, even though today every field
    happens to map 1:1 onto RemediationChange's own columns."""

    id: uuid.UUID
    remediation_run_id: uuid.UUID
    source_issue_id: uuid.UUID
    row_number: int
    column_name: str | None
    action: str
    original_value: str | None
    proposed_value: str | None
    reason: str
    confidence: float
    created_at: datetime

    # Module 16 Phase 3: effective decision status for this change.
    # "pending"  — no decision row exists yet.
    # "approved" — latest decision_timestamp row has decision="approved".
    # "rejected" — latest decision_timestamp row has decision="rejected".
    # Populated by the endpoint via a single batch query over the page;
    # never stored as a column on RemediationChange (the source of truth
    # is always remediation_change_decisions).
    decision_status: str


# ---------------------------------------------------------------------------
# Module 16: Approval Queue decision schemas
# ---------------------------------------------------------------------------

class BulkDecisionResponse(BaseModel):
    """Response for POST .../approve-all and .../reject-all.

    total_changes        — all RemediationChange rows in the run
    approved_count       — rows newly approved by this call (0 on reject-all)
    rejected_count       — rows newly rejected by this call (0 on approve-all)
    skipped_existing_count — rows that already had any decision and were not
                             touched; approved_count + rejected_count +
                             skipped_existing_count == total_changes always.
    """

    total_changes: int
    approved_count: int
    rejected_count: int
    skipped_existing_count: int


class RemediationChangeDecisionRequest(BaseModel):
    """Optional request body for the approve/reject endpoints.

    comment is the only accepted input -- all other fields on
    RemediationChangeDecision (reviewer_id, reviewer_name, reviewer_role,
    decision_timestamp) are populated from the authenticated user's session
    at request time and are never accepted from the caller. This prevents
    callers from spoofing reviewer identity or backdating decisions.

    comment is Text on the DB side (unbounded), intentionally so: if a
    future module encrypts reviewer comments the ciphertext length will
    exceed any VARCHAR limit. No length cap is enforced here either -- the
    API accepts whatever the caller sends; the DB column absorbs it."""

    comment: str | None = None


class RemediationChangeDecisionRead(BaseModel):
    """from_attributes=True because the endpoint returns the ORM row
    directly after a flush/refresh -- same approach as CleaningRunRead,
    StandardizationRunRead, etc. (all the per-run approval DTOs). Unlike
    RemediationChangeRead, no field here requires a cross-table join or
    explicit construction: every column on RemediationChangeDecision maps
    1:1 to a field here."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    remediation_run_id: uuid.UUID
    remediation_change_id: uuid.UUID

    decision: str           # "approved" | "rejected"
    reviewer_id: uuid.UUID | None
    reviewer_name: str | None
    reviewer_role: str | None
    decision_timestamp: datetime

    comment: str | None

    # apply fields -- always None in Module 16; present so callers can
    # distinguish "never applied" from a missing field in a future response.
    applied_at: datetime | None
    applied_by: uuid.UUID | None

    created_at: datetime
