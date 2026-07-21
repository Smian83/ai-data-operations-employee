"""Request/response schemas for MatchRuleSet + MatchRuleField -- the
organization-configured, versioned matching configuration described in
docs/module-8-data-matching-deduplication-design.md Sections 3, 5, 7.

A rule set and its full field list are created together in a single API
call (MatchRuleSetCreate.fields) and are immutable afterward: there is no
update endpoint for either resource, only creation and read (Section 5).
duplicate_threshold/review_threshold ordering is validated here at the
Pydantic layer (a 422 on a malformed configuration) in addition to the
database CHECK constraint, so a malformed threshold configuration can
never reach the matching engine."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.core.validation import normalize_name
from app.models.enums import MATCH_RULE_COMPARISON_TYPES


class MatchRuleFieldCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    column_name: str = Field(min_length=1, max_length=255)
    comparison_type: str
    weight: float = Field(gt=0)

    @field_validator("column_name")
    @classmethod
    def _normalize_column_name(cls, v: str) -> str:
        v = normalize_name(v)
        if not v:
            raise ValueError("column_name must not be blank")
        return v

    @field_validator("comparison_type")
    @classmethod
    def _validate_comparison_type(cls, v: str) -> str:
        if v not in MATCH_RULE_COMPARISON_TYPES:
            raise ValueError(f"comparison_type must be one of {MATCH_RULE_COMPARISON_TYPES}")
        return v


class MatchRuleFieldRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    rule_set_id: uuid.UUID
    column_name: str
    comparison_type: str
    weight: float
    created_at: datetime


class MatchRuleSetCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # None = applies to every data source in this organization.
    data_source_id: uuid.UUID | None = None
    duplicate_threshold: float = Field(ge=0, le=1)
    review_threshold: float = Field(ge=0, le=1)
    # At least one match key is required to create a rule set at all --
    # a rule set with zero configured fields would be indistinguishable
    # from having no rule set (Section 6/7: Stage 2 does not run without
    # at least one configured field).
    fields: list[MatchRuleFieldCreate] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_threshold_order(self) -> "MatchRuleSetCreate":
        if not (self.review_threshold < self.duplicate_threshold):
            raise ValueError(
                "review_threshold must be strictly less than duplicate_threshold"
            )
        return self

    @field_validator("fields")
    @classmethod
    def _validate_unique_columns(cls, v: list[MatchRuleFieldCreate]) -> list[MatchRuleFieldCreate]:
        seen = {f.column_name.strip().lower() for f in v}
        if len(seen) != len(v):
            raise ValueError("column_name must not be configured twice in the same rule set")
        return v


class MatchRuleSetRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    data_source_id: uuid.UUID | None
    version: int
    duplicate_threshold: float
    review_threshold: float
    is_active: bool
    created_by: uuid.UUID | None
    created_at: datetime
    fields: list[MatchRuleFieldRead]
