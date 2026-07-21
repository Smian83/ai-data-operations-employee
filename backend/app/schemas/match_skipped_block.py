"""Read-only API schema for MatchSkippedBlock (new in the approved
design revision) -- one row per block skipped for exceeding
MATCH_MAX_BLOCK_SIZE, closing the "why were these two records never
compared" gap MatchDecision alone cannot answer. See
docs/module-8-data-matching-deduplication-design.md Sections 3, 9, 11."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MatchSkippedBlockRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    match_run_id: uuid.UUID
    blocking_key: str
    block_size: int
    sample_row_indices: list[int]
    created_at: datetime
