"""Read-only API schema for MatchGroup (Section 3/9 of
docs/module-8-data-matching-deduplication-design.md). Full group
membership is reconstructable from MatchDecision rows referencing this
group's id (record_a_row_index/record_b_row_index), not duplicated here."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MatchGroupRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    match_run_id: uuid.UUID
    canonical_row_index: int
    record_count: int
    confidence_score: float
    created_at: datetime
