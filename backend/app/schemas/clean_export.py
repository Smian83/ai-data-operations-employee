"""Read/write API schemas for Module 19 — the Clean Export Engine.

Four schemas:
  CleanExportCreate  -- request body for POST /jobs/{job_id}/exports
  CleanExportRead    -- response DTO for a CleanExport row
  CleanExportListResponse -- paginated list wrapper
  CleanExportDownloadMeta -- lightweight metadata for the download endpoint

No approval state machine: clean exports are immutable once completed
(status = 'completed' | 'failed' | 'blocked'). A new export with a fresh
idempotency_key must be requested to re-export.

artifact_path is deliberately absent from CleanExportRead -- the
download endpoint constructs the path server-side from
(organization_id, artifact_id, format) + the CLEAN_EXPORT_OUTPUT_ROOT
setting. This mirrors Module 10's design (output_file_path stripped from
ExportRunRead) and ensures no filesystem detail is exposed via the API.
"""
import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CleanExportCreate(BaseModel):
    """Request body for POST /jobs/{job_id}/exports.

    format:         'csv' or 'xlsx' -- the output format.
    idempotency_key: caller-generated key (e.g., UUID v4 as a string).
                    Subsequent calls with the same key return the existing
                    export without re-running it.
    dataset_version: optional UUID of a specific QualityControlRun to pin
                    this export to. If omitted, the handler picks the most
                    recent PASS/PASS_WITH_WARNINGS QC run for the data source.
    """
    format: Literal["csv", "xlsx"]
    idempotency_key: str = Field(
        ...,
        min_length=1,
        max_length=255,
        description="Caller-generated idempotency key (e.g., UUID v4 string).",
    )
    dataset_version: uuid.UUID | None = Field(
        default=None,
        description=(
            "Optional QualityControlRun.id to pin this export to. "
            "Defaults to the latest PASS/PASS_WITH_WARNINGS QC run."
        ),
    )


class CleanExportRead(BaseModel):
    """Response DTO for a CleanExport row.

    artifact_path is intentionally absent -- use GET /exports/{id}/download
    to retrieve the artifact. checksum, row_count, and column_count are
    None for non-completed rows (blocked, failed, pending, processing).
    """
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    job_id: uuid.UUID
    data_source_id: uuid.UUID
    dataset_version: uuid.UUID | None

    format: str
    status: str

    # None for non-completed rows.
    artifact_id: uuid.UUID | None
    checksum: str | None
    row_count: int | None
    column_count: int | None

    idempotency_key: str
    failure_reason: str | None

    created_at: datetime
    completed_at: datetime | None
