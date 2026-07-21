"""Read-only API schema for ExportRun. Structural mirror of
StandardizationRunRead (see schemas/standardization_run.py) --
output_file_path/output_sha256 are back, unlike MatchRunRead, since
Export writes a real output file -- plus match_run_id and the
export-specific row/file-metadata counters. export_timestamp is exposed
here as database metadata; it is never present inside the CSV file
itself (see docs/module-9-data-export-engine-design.md's Determinism
Clarification)."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ExportRunRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    organization_id: uuid.UUID
    task_run_id: uuid.UUID
    task_id: uuid.UUID
    data_source_id: uuid.UUID
    source_task_run_id: uuid.UUID
    match_run_id: uuid.UUID
    output_file_path: str
    output_sha256: str
    source_row_count: int
    row_count: int
    excluded_row_count: int
    duplicate_groups_materialized_count: int
    output_file_size_bytes: int
    output_column_count: int
    export_timestamp: datetime
    csv_format_version: int
    export_engine_version: str
    status: str
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    rejected_by: uuid.UUID | None
    rejected_at: datetime | None
    rolled_back_by: uuid.UUID | None
    rolled_back_at: datetime | None
    created_at: datetime
