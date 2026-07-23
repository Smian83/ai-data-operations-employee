"""Task request/response schemas."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.config import get_settings
from app.core.validation import normalize_name
from app.models.enums import TaskType

# Module 12: deprecated. A free-text label only -- has no effect on
# execution. Never read by the scheduler, never parsed, never migrated
# into schedule_interval_seconds. Kept read/write for backward
# compatibility; see docs/module-12-scheduled-task-execution-design.md
# Section 5 for the deprecation/removal strategy.
_SCHEDULE_DEPRECATED_DESCRIPTION = (
    "Deprecated free-text label only. It has no effect on execution. "
    "Use schedule_interval_seconds to enable automatic runs."
)
_SCHEDULE_INTERVAL_SECONDS_DESCRIPTION = (
    "The sole executable scheduling configuration: recurs every N seconds "
    "of elapsed UTC time. Only valid for SYNC tasks. Bounds are runtime-"
    "configurable (MINIMUM_SCHEDULE_INTERVAL_SECONDS / "
    "MAXIMUM_SCHEDULE_INTERVAL_SECONDS)."
)


def _validate_schedule_interval_bounds(v: int | None) -> int | None:
    """Bounds are read from Settings at validation time (not a static
    Field(ge=, le=)), since the operator-facing minimum/maximum are
    runtime-configurable (app.core.config.Settings). A violation here is a
    field-level format/range error -> FastAPI's standard 422, matching the
    existing precedent set by `name`'s own min_length=1 violations."""
    if v is None:
        return v
    settings = get_settings()
    if v < settings.minimum_schedule_interval_seconds:
        raise ValueError(
            f"schedule_interval_seconds must be at least "
            f"{settings.minimum_schedule_interval_seconds} seconds"
        )
    if v > settings.maximum_schedule_interval_seconds:
        raise ValueError(
            f"schedule_interval_seconds must be at most "
            f"{settings.maximum_schedule_interval_seconds} seconds"
        )
    return v


class TaskCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    data_source_id: uuid.UUID | None = None
    description: str | None = None
    task_type: TaskType
    schedule: str | None = Field(
        default=None, max_length=100, description=_SCHEDULE_DEPRECATED_DESCRIPTION
    )
    # Module 12: the sole authoritative, machine-executable scheduling
    # field. See app/api/tasks.py::_validate_schedule_interval_task_type
    # for the SYNC-only, task-type-dependent business rule (a 400, not a
    # 422 -- it depends on another field's value, the same category as
    # source_task_run_id's own validation in create_task_run).
    schedule_interval_seconds: int | None = Field(
        default=None, description=_SCHEDULE_INTERVAL_SECONDS_DESCRIPTION
    )
    # Module 4: per-task execution engine overrides. None means "use the
    # worker's global default" -- never zero/unlimited.
    max_attempts: int | None = Field(default=None, ge=1, le=20)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86400)

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, v: str) -> str:
        v = normalize_name(v)
        if not v:
            raise ValueError("name must not be blank")
        return v

    @field_validator("schedule_interval_seconds")
    @classmethod
    def _validate_schedule_interval_seconds(cls, v: int | None) -> int | None:
        return _validate_schedule_interval_bounds(v)


class TaskUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    data_source_id: uuid.UUID | None = None
    description: str | None = None
    task_type: TaskType | None = None
    schedule: str | None = Field(
        default=None, max_length=100, description=_SCHEDULE_DEPRECATED_DESCRIPTION
    )
    schedule_interval_seconds: int | None = Field(
        default=None, description=_SCHEDULE_INTERVAL_SECONDS_DESCRIPTION
    )
    max_attempts: int | None = Field(default=None, ge=1, le=20)
    timeout_seconds: int | None = Field(default=None, ge=1, le=86400)

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = normalize_name(v)
        if not v:
            raise ValueError("name must not be blank")
        return v

    @field_validator("schedule_interval_seconds")
    @classmethod
    def _validate_schedule_interval_seconds(cls, v: int | None) -> int | None:
        return _validate_schedule_interval_bounds(v)


class TaskRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    data_source_id: uuid.UUID | None
    name: str
    description: str | None
    task_type: TaskType
    schedule: str | None
    schedule_interval_seconds: int | None
    next_run_at: datetime | None
    max_attempts: int | None
    timeout_seconds: int | None
    is_active: bool
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
