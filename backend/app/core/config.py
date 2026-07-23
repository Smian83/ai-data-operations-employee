"""
Application configuration.

All runtime configuration is sourced from environment variables (or a local
.env file during development) via pydantic-settings. Nothing in this module
should hardcode environment-specific values.
"""
from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Module 12: the fixed, non-configurable database safety floor (see
# ck_tasks_schedule_interval_hard_floor in app/models/task.py). Duplicated
# here (not derived programmatically) only as the lower bound for
# minimum_schedule_interval_seconds below -- Alembic migrations cannot read
# Settings at migration-authoring time, so these two "30"s are a
# deliberately, explicitly hand-kept-in-sync pair, not a single source of
# truth. Both must be changed together if this floor is ever revisited.
SCHEDULE_INTERVAL_HARD_FLOOR_SECONDS = 30

# Module 13: the same "fixed, non-configurable database safety floor"
# pattern applied to output artifact retention -- see
# ck_artifact_retention_events_window_days_hard_floor in
# app/models/artifact_retention_event.py
# (RETENTION_WINDOW_HARD_FLOOR_DAYS_DB). Hand-kept-in-sync with that
# constant, not derived, for the identical reason SCHEDULE_INTERVAL_HARD_
# FLOOR_SECONDS is hand-kept-in-sync with its own database CHECK.
RETENTION_WINDOW_HARD_FLOOR_DAYS = 7


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

    # --- Data matching & deduplication engine (Module 8) ---
    # No csv_matched_root setting -- Module 8 produces no output file at
    # all (see docs/module-8-data-matching-deduplication-design.md
    # Section 2's architectural decision); MatchHandler never opens a new
    # file for writing anywhere.
    #
    # A block whose size exceeds this bound is skipped entirely (no
    # pairwise comparisons performed within it); the skip is always
    # surfaced via MatchRun.skipped_block_count and a MatchSkippedBlock
    # audit row, never silent.
    match_max_block_size: int = Field(
        default=1_000, alias="MATCH_MAX_BLOCK_SIZE", gt=0
    )
    # Caps individual MatchDecision rows persisted per run; same
    # bounded-but-never-silent pattern as CLEANING_MAX_PERSISTED_CHANGES/
    # STANDARDIZATION_MAX_PERSISTED_CHANGES -- MatchRun's aggregate counts
    # (duplicate_pairs_count, ambiguous_pairs_count, total_comparisons_
    # count) are always the true totals even when this is capped.
    match_max_persisted_decisions: int = Field(
        default=10_000, alias="MATCH_MAX_PERSISTED_DECISIONS", gt=0
    )
    # Caps the row_index sample recorded per skipped block
    # (MatchSkippedBlock.sample_row_indices) -- block_size on that same
    # row is always the true, uncapped count.
    match_max_skipped_row_sample: int = Field(
        default=20, alias="MATCH_MAX_SKIPPED_ROW_SAMPLE", gt=0
    )

    # --- Data export engine (Module 9) ---
    # Exported (deduplicated) output CSVs are written under this
    # tenant-scoped root (CSV_EXPORTED_ROOT/{organization_id}/...),
    # distinct from CSV_INPUT_ROOT, CSV_OUTPUT_ROOT, and
    # CSV_STANDARDIZED_ROOT -- the Module 7 standardized file being
    # exported is never opened for writing anywhere in this module (see
    # app.worker.handlers.export). First output-writing module since
    # Module 7; Module 8 deliberately wrote no file at all.
    csv_exported_root: str = Field(
        default="./data/csv_exported", alias="CSV_EXPORTED_ROOT"
    )
    # Caps individual ExportRowExclusion rows persisted per run; same
    # bounded-but-never-silent pattern as CLEANING_MAX_PERSISTED_CHANGES/
    # STANDARDIZATION_MAX_PERSISTED_CHANGES/MATCH_MAX_PERSISTED_DECISIONS
    # -- ExportRun.excluded_row_count is always the true total even when
    # this is capped. In practice already bounded transitively by
    # CSV_MAX_ROWS, since exclusions can never exceed the input row count.
    export_max_persisted_exclusions: int = Field(
        default=10_000, alias="EXPORT_MAX_PERSISTED_EXCLUSIONS", gt=0
    )

    # --- Scheduled task execution (Module 12) ---
    # Gates app.worker.scheduler.run_due_schedules() inside the same
    # run_forever() loop that already gates reap_expired_runs() via
    # reaper_poll_interval_seconds -- same type (float), same
    # time.monotonic()-based comparison, same bounded-but-configurable
    # convention as every other worker-loop interval in this file.
    scheduler_poll_interval_seconds: float = Field(
        default=15.0, alias="SCHEDULER_POLL_INTERVAL_SECONDS", ge=1.0, le=300.0
    )
    # Maximum due tasks processed per scheduler pass (a bounded loop of
    # independent single-task transactions -- see app/worker/scheduler.py).
    # Mirrors worker_claim_batch_size's own convention exactly.
    scheduler_claim_batch_size: int = Field(
        default=50, alias="SCHEDULER_CLAIM_BATCH_SIZE", gt=0
    )
    # The real, operator-facing minimum -- enforced in Pydantic on every
    # write to Task.schedule_interval_seconds (app/schemas/task.py). Its
    # own `ge` bound ties it structurally to
    # SCHEDULE_INTERVAL_HARD_FLOOR_SECONDS (the fixed database floor, see
    # app/models/task.py's ck_tasks_schedule_interval_hard_floor): this
    # setting can never be configured below that floor, so the two layers
    # can never end up inconsistent at runtime -- pydantic-settings raises
    # ValidationError at process startup otherwise, exactly like every
    # other bounded setting in this file.
    minimum_schedule_interval_seconds: int = Field(
        default=60,
        alias="MINIMUM_SCHEDULE_INTERVAL_SECONDS",
        ge=SCHEDULE_INTERVAL_HARD_FLOOR_SECONDS,
    )
    # Application-layer-only ceiling -- no corresponding database CHECK,
    # since an overly long interval carries no "catch-up storm" safety risk
    # the way too short an interval does (see
    # ck_tasks_schedule_interval_hard_floor's own docstring). Default: 30
    # days.
    maximum_schedule_interval_seconds: int = Field(
        default=2_592_000, alias="MAXIMUM_SCHEDULE_INTERVAL_SECONDS", gt=0
    )

    @model_validator(mode="after")
    def _validate_schedule_interval_bounds(self) -> "Settings":
        if self.maximum_schedule_interval_seconds < self.minimum_schedule_interval_seconds:
            raise ValueError(
                "MAXIMUM_SCHEDULE_INTERVAL_SECONDS must be greater than or equal to "
                "MINIMUM_SCHEDULE_INTERVAL_SECONDS"
            )
        return self

    # --- Output artifact retention (Module 13) ---
    # Master switch -- every other setting in this block is inert while
    # this is false. Default false: no artifact is ever purged unless an
    # operator has made an explicit, documented decision to enable it. See
    # docs/module-13-output-artifact-retention-design.md Sections 6, 21.
    output_retention_enabled: bool = Field(
        default=False, alias="OUTPUT_RETENTION_ENABLED"
    )
    # The real, operator-facing minimum -- enforced here, at process
    # startup (fail-fast, never silently clamped), the same convention
    # minimum_schedule_interval_seconds already established. Its own `ge`
    # bound ties it structurally to RETENTION_WINDOW_HARD_FLOOR_DAYS (the
    # fixed database floor -- see
    # ck_artifact_retention_events_window_days_hard_floor in
    # app/models/artifact_retention_event.py): this setting can never be
    # configured below that floor, so the two layers can never end up
    # inconsistent at runtime.
    output_retention_window_days: int = Field(
        default=30,
        alias="OUTPUT_RETENTION_WINDOW_DAYS",
        ge=RETENTION_WINDOW_HARD_FLOOR_DAYS,
    )
    # Gates app.worker.retention.purge_expired_artifacts() inside the same
    # run_forever() loop that already gates run_due_schedules() (Module
    # 12) and reap_expired_runs() -- same type (float), same
    # time.monotonic()-based comparison, same bounded-but-configurable
    # convention as every other worker-loop interval in this file.
    # Deliberately a much longer default than the scheduler's own 15s
    # poll interval -- retention is the lowest-urgency background pass in
    # this system; nothing depends on it running promptly.
    retention_poll_interval_seconds: float = Field(
        default=3600.0, alias="RETENTION_POLL_INTERVAL_SECONDS", ge=60.0, le=86400.0
    )
    # Maximum eligible artifacts processed per retention pass (a bounded
    # loop of independent single-artifact transactions -- see
    # app/worker/retention.py). Mirrors worker_claim_batch_size's and
    # scheduler_claim_batch_size's own convention exactly.
    retention_claim_batch_size: int = Field(
        default=50, alias="RETENTION_CLAIM_BATCH_SIZE", gt=0
    )
    # When true, a retention pass evaluates and records every eligible
    # artifact exactly as it would for real, but never actually deletes a
    # file and never sets output_deleted_at on the owning run -- lets an
    # operator observe a policy's impact via artifact_retention_events
    # before ever enabling real deletion. See
    # docs/module-13-output-artifact-retention-design.md's lifecycle
    # section for the full dry-run/PURGE_PENDING interaction.
    output_retention_dry_run: bool = Field(
        default=False, alias="OUTPUT_RETENTION_DRY_RUN"
    )

    @property
    def is_production(self) -> bool:
        return self.app_env == "production"


@lru_cache
def get_settings() -> Settings:
    """Return a cached Settings instance (env is read once per process)."""
    return Settings()
