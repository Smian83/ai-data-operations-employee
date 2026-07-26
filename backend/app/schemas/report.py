"""Read-only API schemas for Module 20 — the DTOs returned by the three
report endpoints:

  GET /tasks/{task_id}/runs/{run_id}/report
  GET /tasks/{task_id}/report
  GET /tasks/{task_id}/runs/{run_id}/report/download   (streaming, no schema)

ReportRunRead is cheap: report_data was pre-computed and stored by
ReportHandler at persist time — no aggregation is performed here.

processing_duration_ms is NOT a column on ReportRun — derived at the API
layer from TaskRun.started_at / TaskRun.finished_at, same decision as every
prior run-summary table (RemediationRun, ValidationRun, QualityControlRun).
"""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class ReportRunRead(BaseModel):
    """Summary DTO for one completed ReportRun.

    The full structured report is embedded in report_data — no separate
    endpoint is needed for the JSON contents (the download endpoint serves
    the same JSON as a streaming attachment). processing_duration_ms is
    the only derived field: computed once per request from
    TaskRun.started_at and TaskRun.finished_at by the endpoint. None when
    finished_at IS NULL (run not yet complete)."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    task_run_id: uuid.UUID
    task_id: uuid.UUID
    data_source_id: uuid.UUID

    # Nullable: None when no QualityControlRun existed at report time.
    quality_control_run_id: uuid.UUID | None

    report_engine_version: str
    report_schema_version: str

    # Full structured report -- see app.reports.engine for schema.
    report_data: dict[str, Any]

    # Derived: (TaskRun.finished_at - TaskRun.started_at) in milliseconds.
    # None when finished_at IS NULL.
    processing_duration_ms: int | None

    created_at: datetime
