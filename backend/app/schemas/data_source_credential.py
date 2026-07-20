"""Write-only schema for setting a DataSource's live credentials. There is
deliberately no corresponding read schema anywhere in the API -- once
written, credentials are only ever readable by the execution engine via
CredentialProvider, never returned in any HTTP response."""
from pydantic import BaseModel, ConfigDict, field_validator


class DataSourceCredentialSet(BaseModel):
    model_config = ConfigDict(extra="forbid")

    credentials: dict

    @field_validator("credentials")
    @classmethod
    def _non_empty(cls, value: dict) -> dict:
        if not value:
            raise ValueError("credentials must not be empty")
        return value
