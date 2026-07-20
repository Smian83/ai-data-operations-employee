"""Read-only schema for the Module 4 execution audit trail
(TaskRunEvent). No create/update schemas exist -- these rows are written
only by the execution engine itself, never via any client-facing input."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class TaskRunEventRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    task_run_id: uuid.UUID
    event_type: str
    from_status: str | None
    to_status: str | None
    worker_id: str | None
    detail: dict
    created_at: datetime
