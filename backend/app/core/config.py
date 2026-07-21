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

    # --- Worker / execution engine (Module 4) ---
    # Fernet key (32 url-safe base64-encoded bytes) used to encrypt
    # DataSourceCredential rows at the application layer. Generate with:
    #   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    # There is no insecure default in production: DatabaseCredentialProvider
    # refuses to start if this is unset and APP_ENV=production.
    credential_encryption_key: str | None = Field(
        default=None, alias="CREDENTIAL_ENCRYPTION_KEY"
    )
    worker_id: str = Field(default="worker-1", alias="WORKER_ID")
    worker_claim_batch_size: int = Field(default=5, alias="WORKER_CLAIM_BATCH_SIZE")
    worker_poll_interval_seconds: float = Field(
        default=5.0, alias="WORKER_POLL_INTERVAL_SECONDS"
    )
    worker_heartbeat_interval_seconds: float = Field(
        default=30.0, alias="WORKER_HEARTBEAT_INTERVAL_SECONDS"
    )
    worker_default_timeout_seconds: int = Field(
        default=300, alias="WORKER_DEFAULT_TIMEOUT_SECONDS"
    )
    worker_default_max_attempts: int = Field(
        default=3, alias="WORKER_DEFAULT_MAX_ATTEMPTS"
    )
    worker_retry_base_delay_seconds: int = Field(
        default=30, alias="WORKER_RETRY_BASE_DELAY_SECONDS"
    )
    worker_retry_max_delay_seconds: int = Field(
        default=900, alias="WORKER_RETRY_MAX_DELAY_SECONDS"
    )
    reaper_poll_interval_seconds: float = Field(
        default=15.0, alias="REAPER_POLL_INTERVAL_SECONDS"
    )

    # --- Data ingestion and profiling (Module 5) ---
    # CSV file paths in DataSource.connection_metadata.file_path are always
    # resolved relative to this server-controlled root -- never absolute,
    # never able to escape it (see app.profiling.csv_loader.resolve_source_path).
    csv_input_root: str = Field(default="./data/csv", alias="CSV_INPUT_ROOT")
    csv_max_file_size_bytes: int = Field(
        default=25 * 1024 * 1024, alias="CSV_MAX_FILE_SIZE_BYTES", gt=0
    )
    csv_max_rows: int = Field(default=100_000, alias="CSV_MAX_ROWS", gt=0)
    csv_max_columns: int = Field(default=500, alias="CSV_MAX_COLUMNS", gt=0)
    csv_max_cell_length: int = Field(
        default=100_000, alias="CSV_MAX_CELL_LENGTH", gt=0
    )
    csv_max_distinct_values: int = Field(
        default=100, alias="CSV_MAX_DISTINCT_VALUES", gt=0
    )
    csv_max_sample_values: int = Field(
        default=10, alias="CSV_MAX_SAMPLE_VALUES", gt=0
    )

    # --- Data cleaning engine (Module 6) ---
    # Cleaned output CSVs are written under this tenant-scoped root
    # (CSV_OUTPUT_ROOT/{organization_id}/...), mirroring CSV_INPUT_ROOT's
    # existing per-tenant isolation exactly -- never the same root as
    # CSV_INPUT_ROOT, and the original source file is never opened for
    # writing anywhere in this module (see app.worker.handlers.cleaning).
    csv_output_root: str = Field(default="./data/csv_cleaned", alias="CSV_OUTPUT_ROOT")
    # Caps individual CleaningChange rows persisted per run; CleaningRun.
    # total_changes_count is always the true total even when capped -- same
    # bounded-but-never-silent pattern as CSV_MAX_DISTINCT_VALUES/
    # CSV_MAX_SAMPLE_VALUES.
    cleaning_max_persisted_changes: int = Field(
        default=10_000, alias="CLEANING_MAX_PERSISTED_CHANGES", gt=0
    )

    # --- Data standardization engine (Module 7) ---
    # Standardized output CSVs are written under this tenant-scoped root
    # (CSV_STANDARDIZED_ROOT/{organization_id}/...), distinct from BOTH
    # CSV_INPUT_ROOT and CSV_OUTPUT_ROOT -- the Module 6 output being
    # standardized is never opened for writing anywhere in this module
    # (see app.worker.handlers.standardization).
    csv_standardized_root: str = Field(
        default="./data/csv_standardized", alias="CSV_STANDARDIZED_ROOT"
    )
    # Caps individual StandardizationChange rows persisted per run; same
    # bounded-but-never-silent pattern as CLEANING_MAX_PERSISTED_CHANGES.
    standardization_max_persisted_changes: int = Field(
        default=10_000, alias="STANDARDIZATION_MAX_PERSISTED_CHANGES", gt=0
    )

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (env is read once per process)."""
    return Settings()
