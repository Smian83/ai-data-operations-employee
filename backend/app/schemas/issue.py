"""Read-only API schema for Issue -- the per-finding DTO
GET /tasks/{task_id}/runs/{run_id}/detection/issues returns, paginated
and filterable (Module 14 Phase 3).

dataset_id is NOT a column on app.models.issue.Issue (see that model's
own docstring): it is populated here, at the API layer, from the parent
IssueDetectionRun.data_source_id every returned Issue belongs to --
app.api.tasks.list_task_run_detection_issues looks the parent run up once
per request and stamps its data_source_id onto every IssueRead in the
page, rather than denormalizing the column onto Issue itself. This is a
plain BaseModel (not from_attributes=True) precisely because it is never
built directly from an Issue ORM instance -- every field is set
explicitly by the endpoint, which is also how "no SQLAlchemy model is
ever returned directly" (Phase 3 correction #2) is enforced here."""
import uuid
from datetime import datetime

from pydantic import BaseModel


class IssueRead(BaseModel):
    id: uuid.UUID
    detection_run_id: uuid.UUID
    dataset_id: uuid.UUID
    row_number: int
    column_name: str | None
    issue_type: str
    severity: str
    original_value: str | None
    suggested_fix: str | None
    confidence: float
    created_at: datetime
