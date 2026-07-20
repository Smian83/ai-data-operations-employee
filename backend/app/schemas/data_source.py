"""DataSource request/response schemas."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.core.validation import find_secret_like_key, normalize_name
from app.models.enums import SourceType


def _validate_connection_metadata(v: dict) -> dict:
    offending_path = find_secret_like_key(v)
    if offending_path:
        raise ValueError(
            f"connection_metadata field '{offending_path}' looks like a secret "
            "(password/token/key/etc.). Do not store credentials here — a "
            "future encrypted secrets module will handle real credentials."
        )
    return v


class DataSourceCreate(BaseModel):
    # extra="forbid": organization_id / created_by / is_active can NEVER be
    # supplied by the client, on this or any Module 3 create/update schema.
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=255)
    source_type: SourceType
    connection_metadata: dict = Field(default_factory=dict)

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, v: str) -> str:
        v = normalize_name(v)
        if not v:
            raise ValueError("name must not be blank")
        return v

    @field_validator("connection_metadata")
    @classmethod
    def _no_secrets(cls, v: dict) -> dict:
        return _validate_connection_metadata(v)


class DataSourceUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=255)
    source_type: SourceType | None = None
    connection_metadata: dict | None = None

    @field_validator("name")
    @classmethod
    def _normalize_name(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = normalize_name(v)
        if not v:
            raise ValueError("name must not be blank")
        return v

    @field_validator("connection_metadata")
    @classmethod
    def _no_secrets(cls, v: dict | None) -> dict | None:
        if v is None:
            return None
        return _validate_connection_metadata(v)


class DataSourceRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    name: str
    source_type: SourceType
    connection_metadata: dict
    is_active: bool
    created_by: uuid.UUID | None
    created_at: datetime
    updated_at: datetime
