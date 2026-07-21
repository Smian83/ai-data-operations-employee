"""Read-only API schema for ExportRowExclusion (Section 7/9 of
docs/module-9-data-export-engine-design.md). Answers "why is this row
missing from my exported file" -- cross-reference match_group_id against
GET .../matching/decisions?match_group_id=... for the deeper "why was
this row grouped" question, already answered by Module 8's own audit
trail and deliberately not duplicated here."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ExportRowExclusionRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    export_run_id: uuid.UUID
    row_index: int
    match_group_id: uuid.UUID
    canonical_row_index: int
    reason: str
    rule_version: str
    created_at: datetime
