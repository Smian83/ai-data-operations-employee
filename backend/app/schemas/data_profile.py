"""Read-only API schema for persisted CSV profiling results."""
import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict


class DataProfileRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    task_run_id: uuid.UUID
    task_id: uuid.UUID
    data_source_id: uuid.UUID
    source_filename: str
    source_size_bytes: int
    source_sha256: str
    detected_encoding: str
    delimiter: str
    row_count: int
    column_count: int
    duplicate_row_count: int
    missing_value_total: int
    column_profiles: list[dict[str, Any]]
    structural_issues: list[dict[str, Any]]
    limits_applied: dict[str, int]
    profiled_at: datetime
