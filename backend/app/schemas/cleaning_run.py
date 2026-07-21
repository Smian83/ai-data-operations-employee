"""Read-only API schema for CleaningRun."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CleaningRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    task_run_id: uuid.UUID
    task_id: uuid.UUID
    data_source_id: uuid.UUID
    source_task_run_id: uuid.UUID
    output_file_path: str
    output_sha256: str
    row_count: int
    total_changes_count: int
    changes_by_rule: dict[str, int]
    duplicate_row_count: int
    confidence_score: float
    post_clean_row_count: int
    post_clean_missing_value_total: int
    post_clean_duplicate_row_count: int
    cleaning_engine_version: str
    status: str
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    rejected_by: uuid.UUID | None
    rejected_at: datetime | None
    rolled_back_by: uuid.UUID | None
    rolled_back_at: datetime | None
    created_at: datetime
