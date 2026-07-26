"""Read-only API schemas for Module 17 Phase 4 -- the two DTOs that
GET /tasks/{task_id}/runs/{run_id}/validation and
GET /tasks/{task_id}/runs/{run_id}/validation/results return.

Same shape convention as app.schemas.remediation: the summary DTO
(ValidationRunRead) is cheap because every count on it was already
computed by app.worker.handlers.validation at persist time -- no
aggregation is performed here beyond the single derived field documented
below.

processing_duration_ms is NOT a column on ValidationRun -- per the same
decision as RemediationRun (see that model's own docstring): derived at
the API layer from TaskRun.started_at / TaskRun.finished_at (both
already recorded by app.worker.engine for every TaskRun, regardless of
task_type) -- (finished_at - started_at) in whole milliseconds, or None
if the run has not finished yet (finished_at IS NULL). In practice a
ValidationRun row is only ever written after the handler and the TaskRun
both complete, so None is never observable through this endpoint for a
well-formed run.

ValidationResultRead is a plain BaseModel (not from_attributes=True),
same reasoning as IssueRead and RemediationChangeRead: every field is
set explicitly by the endpoint from the ORM row rather than built
directly off a returned SQLAlchemy instance, so no ORM object is ever
returned directly from any endpoint using this schema."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ValidationRunRead(BaseModel):
    """Summary DTO for one completed ValidationRun.

    All count fields (approved_changes_considered, passed_count,
    failed_count, skipped_count) are pre-computed by ValidationHandler at
    persist time -- this schema never aggregates ValidationResult rows.
    results_by_rule is the {rule_name: count} breakdown stored on the
    ValidationRun row itself, also pre-computed.

    processing_duration_ms is the only derived field: computed once per
    request from TaskRun.started_at and TaskRun.finished_at by the
    endpoint (see app.api.tasks.get_task_run_validation). Never stored in
    the database."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    task_run_id: uuid.UUID
    task_id: uuid.UUID
    data_source_id: uuid.UUID
    remediation_run_id: uuid.UUID

    approved_changes_considered: int
    passed_count: int
    failed_count: int
    skipped_count: int

    # {validation_rule_name: count} -- only rules with at least one result
    # are present; empty dict when approved_changes_considered == 0.
    results_by_rule: dict[str, int]

    validation_engine_version: str

    # Derived from TaskRun.started_at / TaskRun.finished_at; None when
    # finished_at IS NULL (run not yet complete).
    processing_duration_ms: int | None

    created_at: datetime


class ValidationResultRead(BaseModel):
    """Per-change validation outcome DTO.

    Plain BaseModel (not from_attributes=True): every field is set
    explicitly by list_task_run_validation_results from the ORM row --
    same "no SQLAlchemy model is ever returned directly" pattern
    IssueRead and RemediationChangeRead both follow. All fields map 1:1
    to ValidationResult columns, so no cross-table join is needed to
    populate any field here (source_issue_id is denormalized on
    ValidationResult for exactly this reason)."""

    id: uuid.UUID
    validation_run_id: uuid.UUID
    remediation_run_id: uuid.UUID
    remediation_change_id: uuid.UUID
    source_issue_id: uuid.UUID

    validation_rule: str
    validation_rule_version: str
    outcome: str
    reason: str

    original_value: str | None
    proposed_value: str | None

    validation_engine_version: str
    created_at: datetime
