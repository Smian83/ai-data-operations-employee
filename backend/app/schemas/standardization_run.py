"""Read-only API schema for StandardizationRun. Direct structural mirror
of CleaningRunRead (see schemas/cleaning_run.py); cleaning_engine_version
is renamed standardization_engine_version and the cleaning-specific
duplicate/missing-value counters are dropped since Module 7 has no
equivalent of them."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class StandardizationRunRead(BaseModel):
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
    confidence_score: float
    standardization_engine_version: str
    status: str
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    rejected_by: uuid.UUID | None
    rejected_at: datetime | None
    rolled_back_by: uuid.UUID | None
    rolled_back_at: datetime | None
    created_at: datetime
