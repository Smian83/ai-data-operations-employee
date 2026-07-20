"""TaskRun response schema. No create-body schema exists — POST
/tasks/{id}/runs takes an empty body; task_id, organization_id, and
triggered_by are always server-derived, never client-supplied."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.enums import TaskRunStatus


class TaskRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_id: uuid.UUID
    organization_id: uuid.UUID
    status: TaskRunStatus
    triggered_by: uuid.UUID | None
    started_at: datetime | None
    finished_at: datetime | None
    log_output: str | None
    error_message: str | None
    created_at: datetime
