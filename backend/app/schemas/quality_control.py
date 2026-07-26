"""Read-only API schemas for Module 18 Phase 3 — the two DTOs that
GET /tasks/{task_id}/runs/{run_id}/quality-control and
GET /tasks/{task_id}/runs/{run_id}/quality-control/findings return.

Same shape convention as app.schemas.validation: the summary DTO
(QualityControlRunRead) is cheap because every count was already computed
by app.worker.handlers.quality_control at persist time — no aggregation
is performed here beyond the single derived field documented below.

processing_duration_ms is NOT a column on QualityControlRun — derived at
the API layer from TaskRun.started_at / TaskRun.finished_at (both already
recorded by app.worker.engine for every TaskRun, regardless of task_type).
Same decision as RemediationRun and ValidationRun.

QualityFindingRead is a plain BaseModel (not from_attributes=True), same
reasoning as IssueRead, RemediationChangeRead, and ValidationResultRead:
every field is set explicitly by the endpoint from the ORM row rather than
built directly off a returned SQLAlchemy instance.
"""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class QualityControlRunRead(BaseModel):
    """Summary DTO for one completed QualityControlRun.

    All count and category fields are pre-computed by QualityControlHandler
    at persist time — this schema never aggregates QualityFinding rows.
    overall_score is nullable: None when no quality categories were
    applicable (in which case release_recommendation is always 'FAIL').

    processing_duration_ms is the only derived field: computed once per
    request from TaskRun.started_at and TaskRun.finished_at by the
    endpoint.  None when finished_at IS NULL (run not yet complete)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    task_run_id: uuid.UUID
    task_id: uuid.UUID
    data_source_id: uuid.UUID
    validation_run_id: uuid.UUID
    remediation_run_id: uuid.UUID
    issue_detection_run_id: uuid.UUID
    data_profile_id: uuid.UUID

    quality_engine_version: str

    # None when no applicable categories (always FAIL in that case).
    overall_score: float | None
    release_recommendation: str

    total_findings: int
    blocking_count: int
    warning_count: int
    info_count: int

    # Pre-computed per-category breakdowns (stored as JSON on the run row).
    # category_scores:        {category: float}  — absent = skipped
    # category_statuses:      {category: str}    — all 8 categories present
    # category_weights_used:  {category: float}  — only applicable categories
    category_scores: dict[str, Any]
    category_statuses: dict[str, str]
    category_weights_used: dict[str, Any]

    # Computed effective post-remediation statistics (Section 3 of design doc).
    post_remediation_stats: dict[str, Any]

    # Derived: (TaskRun.finished_at - TaskRun.started_at) in milliseconds.
    # None when finished_at IS NULL.
    processing_duration_ms: int | None

    created_at: datetime


class QualityFindingRead(BaseModel):
    """Per-finding quality DTO.

    Plain BaseModel (not from_attributes=True): every field is set
    explicitly by list_task_run_quality_control_findings from the ORM row
    — same "no SQLAlchemy model is ever returned directly" pattern
    IssueRead, RemediationChangeRead, and ValidationResultRead all follow.
    All fields map 1:1 to QualityFinding columns."""

    id: uuid.UUID
    quality_control_run_id: uuid.UUID

    category: str
    rule_name: str
    rule_version: str

    severity: str   # 'info' | 'warning' | 'blocking'
    outcome: str    # 'passed' | 'failed' | 'skipped'
    reason: str

    affected_row_count: int | None
    affected_column: str | None

    # Provenance links — all nullable; set only when traceable to a specific
    # upstream row.
    source_issue_id: uuid.UUID | None
    remediation_change_id: uuid.UUID | None
    validation_result_id: uuid.UUID | None

    quality_engine_version: str
    created_at: datetime
