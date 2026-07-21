"""Read-only API schema for MatchDecision. Every field the acceptance
criteria require a match decision to record is present and independently
addressable: source record identifiers (record_a_row_index/
record_b_row_index), duplicate group identifier (match_group_id),
compared fields/normalized values/field-level scores (field_comparisons),
rule applied (rule_name), total score, threshold used, decision,
confidence, reason, rule version, timestamp -- plus blocking_key (the
approved design revision), the direct, inspectable reason the pair was
ever compared at all. See
docs/module-8-data-matching-deduplication-design.md Sections 3, 9."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class MatchDecisionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    match_run_id: uuid.UUID
    match_group_id: uuid.UUID | None
    record_a_row_index: int
    record_b_row_index: int
    blocking_key: str | None
    rule_name: str
    field_comparisons: dict
    total_score: float
    threshold_used: float
    decision: str
    confidence_score: float
    reason: str
    rule_version: str
    created_at: datetime
