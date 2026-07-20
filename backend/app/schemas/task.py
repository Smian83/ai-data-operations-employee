"""Task request/response schemas."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.validation import normalize_name
from app.models.enums import TaskType


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    data_source_id: uuid.UUID | None = None
    description: str | None = None
    task_type: TaskType
    schedule: str | None = Field(default=None, max_length=100)

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, v: str) -> str:
        v = normalize_name(v)
        if not v:
            raise ValueError("name must not be blank")
        return v


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    data_source_id: uuid.UUID | None = None
    description: str | None = None
    task_type: TaskType | None = None
    schedule: str | None = Field(default=None, max_length=100)

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = normalize_name(v)
        if not v:
            raise ValueError("name must not be blank")
        return v


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    data_source_id: uuid.UUID | None
    name: str
    description: str | None
    task_type: TaskType
    schedule: str | None
    is_active: bool
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
