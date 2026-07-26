"""Module 19 shared dataclasses and result types.

All types are frozen dataclasses (immutable). No SQLAlchemy or I/O
dependencies -- this module is safe to import anywhere.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class CleanExportRequest:
    """Describes what to export: format, idempotency key, and optionally
    a specific QualityControlRun version to pin this export to.

    organization_id and job_id are resolved by the handler from the
    authenticated user context and URL parameter respectively.
    """
    organization_id: uuid.UUID
    job_id: uuid.UUID           # task_id in this project
    data_source_id: uuid.UUID
    format: str                 # "csv" | "xlsx"
    idempotency_key: str
    # If supplied, export is pinned to this specific QualityControlRun.
    # If None, the handler finds the latest PASS/PASS_WITH_WARNINGS run.
    dataset_version: uuid.UUID | None = None


@dataclass(frozen=True)
class EligibilityResult:
    """Outcome of the eligibility check.

    eligible=True means all gates passed and export may proceed.
    eligible=False means export is blocked; failure_reason describes why.
    """
    eligible: bool
    # The approved ExportRun.id (Gates 1-2). Always set when eligible=True.
    export_run_id: uuid.UUID | None
    # The output_file_path of the approved ExportRun.
    export_run_artifact_path: str | None
    # The QualityControlRun.id that authorized this export (Gate 3).
    quality_control_run_id: uuid.UUID | None
    # Human-readable explanation of why export is blocked (set when eligible=False).
    failure_reason: str | None = None
    # Gate 4 (APPLY_REMEDIATIONS): the AppliedRemediationRun.id whose artifact
    # will be loaded instead of the raw ExportRun artifact. None when no
    # AppliedRemediationRun exists (Gate 4 is not yet enforced in that case).
    applied_remediation_run_id: uuid.UUID | None = None
    # The output_file_path of the AppliedRemediationRun artifact.
    applied_remediation_run_artifact_path: str | None = None
    # The output_sha256 of the AppliedRemediationRun artifact.
    applied_remediation_run_sha256: str | None = None


@dataclass(frozen=True)
class LoadedDataset:
    """The in-memory representation of the dataset artifact loaded for export.

    When an AppliedRemediationRun exists (the new pipeline), this is loaded
    from the remediated artifact. Otherwise (legacy path), from the ExportRun
    artifact.

    headers: column names in order.
    rows: list of rows, each a list of cell strings (same length as headers).
    source_export_run_id: the ExportRun.id that is the lineage root. Always set
      (whether the artifact was loaded from ExportRun or AppliedRemediationRun).
    source_sha256: SHA-256 of the actual artifact bytes loaded (used in audit).
    """
    headers: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    source_export_run_id: uuid.UUID
    source_sha256: str


@dataclass(frozen=True)
class CleanExportResult:
    """The complete result of a successful clean export.

    artifact_id: server-generated UUID used to construct the on-disk path.
    checksum: SHA-256 hex digest of the artifact bytes.
    row_count: number of data rows (excluding header).
    column_count: number of columns.
    artifact_path: absolute path where the artifact was written.
    format: "csv" | "xlsx".
    quality_control_run_id: the QC run that authorized this export.
    export_run_id: the Module 9 ExportRun whose data was exported.
    """
    artifact_id: uuid.UUID
    checksum: str
    row_count: int
    column_count: int
    artifact_path: str
    format: str
    quality_control_run_id: uuid.UUID
    export_run_id: uuid.UUID
    completed_at: datetime
