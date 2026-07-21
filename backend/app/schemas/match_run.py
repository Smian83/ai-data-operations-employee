"""Read-only API schema for MatchRun. Structural mirror of
StandardizationRunRead (see schemas/standardization_run.py), minus
output_file_path/output_sha256 (Module 8 produces no output file --
design doc Section 2), plus rule_set_id/rule_set_version and the
matching-specific aggregate counters."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MatchRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    task_run_id: uuid.UUID
    task_id: uuid.UUID
    data_source_id: uuid.UUID
    source_task_run_id: uuid.UUID
    rule_set_id: uuid.UUID | None
    rule_set_version: int | None
    row_count: int
    total_comparisons_count: int
    duplicate_group_count: int
    duplicate_pairs_count: int
    ambiguous_pairs_count: int
    skipped_block_count: int
    decisions_by_rule: dict[str, int]
    confidence_score: float
    match_engine_version: str
    status: str
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    rejected_by: uuid.UUID | None
    rejected_at: datetime | None
    rolled_back_by: uuid.UUID | None
    rolled_back_at: datetime | None
    created_at: datetime
