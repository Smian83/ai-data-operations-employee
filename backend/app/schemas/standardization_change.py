"""Read-only API schema for StandardizationChange. Direct structural
mirror of CleaningChangeRead (see schemas/cleaning_change.py), plus the
two fields CleaningChange didn't need -- field_type and rule_version
(see models/standardization_change.py)."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class StandardizationChangeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    standardization_run_id: uuid.UUID
    row_index: int
    column_name: str
    field_type: str
    original_value: str
    standardized_value: str
    rule_name: str
    rule_version: str
    reason: str
    confidence_score: float
    created_at: datetime
