"""TaskRun request/response schemas.

Module 6 update: POST /tasks/{id}/runs now accepts an OPTIONAL body
(TaskRunCreate) instead of always taking none. Existing callers that send
no body at all are completely unaffected -- FastAPI treats a Pydantic-
model body parameter defaulted to None as optional, so an absent body
still resolves to payload=None exactly as before Module 6. source_task_
run_id is only meaningful (and required) for TRANSFORM tasks; task_id,
organization_id, and triggered_by remain always server-derived, never
client-supplied, for every task type.

Module 4 additions (attempt_count, next_retry_at, idempotency_key) are
read-only visibility into the execution engine's bookkeeping.
lease_token, lease_expires_at, and last_heartbeat_at are deliberately NOT
exposed here -- they are internal worker-ownership details, not something
an API client needs or should be able to observe/infer timing from."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict

from app.models.enums import TaskRunStatus


class TaskRunCreate(BaseModel):
    """Optional request body for POST /tasks/{id}/runs. Required for
    TRANSFORM tasks (identifies which prior SYNC run's DataProfile to
    clean); rejected for every other task type."""

    source_task_run_id: uuid.UUID | None = None


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
    attempt_count: int
    next_retry_at: datetime | None
    idempotency_key: uuid.UUID
    source_task_run_id: uuid.UUID | None
