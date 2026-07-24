"""Read-only API schema for IssueDetectionRun -- the cheap summary DTO
GET /tasks/{task_id}/runs/{run_id}/detection returns (Module 14 Phase 3
correction #5: summary endpoints must use aggregated counts, never load
every Issue row). Every field here is copied directly off the already-
computed IssueDetectionRun row app.worker.handlers.issue_detection wrote
-- this schema performs no aggregation of its own, same "cheap because the
work already happened in the worker" shape as CleaningRunRead/
StandardizationRunRead."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class IssueDetectionRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    task_run_id: uuid.UUID
    task_id: uuid.UUID
    data_source_id: uuid.UUID
    rows_scanned: int
    columns_scanned: int
    total_issues_found: int
    persisted_issue_count: int
    issues_by_severity: dict[str, int]
    issues_by_type: dict[str, int]
    limits_applied: dict
    detection_engine_version: str
    detected_at: datetime
