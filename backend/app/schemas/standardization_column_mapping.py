"""Request/response schemas for StandardizationColumnMapping -- the
organization-configured column -> field_type override described in
docs/module-7-data-standardization-engine-design.md Sections 3 and 10.

field_type is validated against the fixed STANDARDIZATION_FIELD_TYPES
registry here at the Pydantic layer (a 422 on an unknown value, never a
silent no-op) rather than as a native Postgres enum -- same "small
internal/config-owned value set -> plain string, validated at the API
layer" precedent models/enums.py already documents for this field."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.validation import normalize_name
from app.models.enums import STANDARDIZATION_FIELD_TYPES


class StandardizationColumnMappingCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # None = applies to every data source in this organization.
    data_source_id: uuid.UUID | None = None
    column_name: str = Field(min_length=1, max_length=255)
    field_type: str

    @field_validator("column_name")
    @classmethod
    def _normalize_column_name(cls, v: str) -> str:
        v = normalize_name(v)
        if not v:
            raise ValueError("column_name must not be blank")
        return v

    @field_validator("field_type")
    @classmethod
    def _validate_field_type(cls, v: str) -> str:
        if v not in STANDARDIZATION_FIELD_TYPES:
            raise ValueError(
                f"field_type must be one of {STANDARDIZATION_FIELD_TYPES}"
            )
        return v


class StandardizationColumnMappingRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    data_source_id: uuid.UUID | None
    column_name: str
    field_type: str
    is_active: bool
    created_by: uuid.UUID | None
    created_at: datetime
