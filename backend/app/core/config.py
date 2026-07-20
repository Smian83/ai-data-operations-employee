"""
Application configuration.

All runtime configuration is sourced from environment variables (or a local
.env file during development) via pydantic-settings. Nothing in this module
should hardcode environment-specific values.
"""
from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Central application settings, populated from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Application ---
    app_name: str = Field(default="AI Data Operations Employee", alias="APP_NAME")
    app_env: Literal["development", "staging", "production"] = Field(
        default="development", alias="APP_ENV"
    )
    app_debug: bool = Field(default=False, alias="APP_DEBUG")

    # --- Server ---
    host: str = Field(default="0.0.0.0", alias="HOST")
    port: int = Field(default=8000, alias="PORT")

    # --- Database ---
    database_url: str = Field(
        default="sqlite:///./local_dev.db",
        alias="DATABASE_URL",
        description="SQLAlchemy connection string. Defaults to local SQLite "
        "ONLY when unset; production must always set DATABASE_URL to Postgres.",
    )

    # --- Logging ---
    log_level: str = Field(default="INFO", alias="LOG_LEVEL")
    log_format: Literal["json", "console"] = Field(default="json", alias="LOG_FORMAT")

    # --- Security ---
    secret_key: str = Field(
        default="insecure-dev-secret-change-me", alias="SECRET_KEY"
    )

    # --- JWT / Auth (Module 2) ---
    jwt_algorithm: str = Field(default="HS256", alias="JWT_ALGORITHM")
    access_token_expire_minutes: int = Field(
        default=60, alias="ACCESS_TOKEN_EXPIRE_MINUTES"
    )

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (env is read once per process)."""
    return Settings()
