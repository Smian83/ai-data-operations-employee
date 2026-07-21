"""Request/response schemas for StandardizationLookupEntry -- the
organization-supplied lookup-table entries described in
docs/module-7-data-standardization-engine-design.md Sections 3 and 6
(abbreviation expansions, canonical company suffixes, country-name
variants, etc.). field_type is optional (None = applies across any
classified field, the generic abbreviation pass); when present it is
validated against the same fixed STANDARDIZATION_FIELD_TYPES registry as
StandardizationColumnMappingCreate.field_type."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.validation import normalize_name
from app.models.enums import STANDARDIZATION_FIELD_TYPES


class StandardizationLookupEntryCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # None = applies across any classified field (generic abbreviations).
    field_type: str | None = None
    lookup_key: str = Field(min_length=1, max_length=255)
    lookup_value: str = Field(min_length=1, max_length=255)

    @field_validator("field_type")
    @classmethod
    def _validate_field_type(cls, v: str | None) -> str | None:
        if v is not None and v not in STANDARDIZATION_FIELD_TYPES:
            raise ValueError(
                f"field_type must be one of {STANDARDIZATION_FIELD_TYPES}"
            )
        return v

    @field_validator("lookup_key", "lookup_value")
    @classmethod
    def _normalize(cls, v: str) -> str:
        v = normalize_name(v)
        if not v:
            raise ValueError("must not be blank")
        return v


class StandardizationLookupEntryRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    field_type: str | None
    lookup_key: str
    lookup_value: str
    is_active: bool
    created_by: uuid.UUID | None
    created_at: datetime
