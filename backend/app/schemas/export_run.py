"""Read-only API schema for ExportRun. Structural mirror of
StandardizationRunRead (see schemas/standardization_run.py) --
output_sha256 is back, unlike MatchRunRead, since Export writes a real
output file -- plus match_run_id and the export-specific row/file-
metadata counters. export_timestamp is exposed here as database
metadata; it is never present inside the CSV file itself (see
docs/module-9-data-export-engine-design.md's Determinism Clarification).

Module 10: output_file_path is deliberately NOT exposed here (removed
per architectural review -- see
docs/module-10-artifact-retrieval-design.md Section 13). Repository
inspection at removal time found exactly three API-response-shape test
assertions depending on it, all in tests/test_export_api.py, and zero
frontend/README/internal-service dependency; those three assertions
were updated to use the ORM row directly, the same pattern already used
everywhere else in the test suite for this field. Use
GET /tasks/{task_id}/runs/{run_id}/export/download to retrieve the
artifact's bytes instead."""
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
