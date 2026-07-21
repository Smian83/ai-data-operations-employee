"""Read-only API schema for CleaningChange."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class CleaningChangeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    cleaning_run_id: uuid.UUID
    row_index: int
    column_name: str
    original_value: str
    cleaned_value: str
    rule_name: str
    reason: str
    confidence_score: float
    created_at: datetime
