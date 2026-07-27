"""Pydantic schemas for Module 21 — Business Rules Engine API."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------------
# Global defaults
# ---------------------------------------------------------------------------

class GlobalDefaultOut(BaseModel):
    id: uuid.UUID
    rule_key: str
    rule_type: str
    rule_source: str
    rule_value: Any
    description: str | None
    introduced_version: str
    deprecated: bool
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Rule set items
# ---------------------------------------------------------------------------

class RuleSetItemCreate(BaseModel):
    rule_key: str = Field(..., max_length=255)
    rule_type: str = Field(..., max_length=50)
    rule_value: Any


class RuleSetItemUpdate(BaseModel):
    rule_value: Any


class RuleSetItemOut(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    rule_set_id: uuid.UUID
    rule_key: str
    rule_type: str
    rule_source: str
    rule_value: Any
    created_at: datetime

    model_config = {"from_attributes": True}


# ---------------------------------------------------------------------------
# Rule sets
# ---------------------------------------------------------------------------

class RuleSetCreate(BaseModel):
    name: str = Field(..., max_length=255)
    description: str | None = None
    data_source_id: uuid.UUID | None = None


class RuleSetOut(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    data_source_id: uuid.UUID | None
    version: int
    name: str
    description: str | None
    is_draft: bool
    is_active: bool
    rule_schema_version: str
    published_at: datetime | None
    published_by_identity: str | None
    created_by_identity: str | None
    created_at: datetime

    model_config = {"from_attributes": True}


class RuleSetDetailOut(RuleSetOut):
    items: list[RuleSetItemOut] = []


# ---------------------------------------------------------------------------
# Rule set runs
# ---------------------------------------------------------------------------

class RuleSetRunOut(BaseModel):
    id: uuid.UUID
    organization_id: uuid.UUID
    rule_set_id: uuid.UUID | None
    rule_set_version: int | None
    rule_schema_version: str
    resolver_version: str
    pipeline_run_type: str
    pipeline_run_id: uuid.UUID
    resolved_rules_sha256: str
    created_at: datetime

    model_config = {"from_attributes": True}
