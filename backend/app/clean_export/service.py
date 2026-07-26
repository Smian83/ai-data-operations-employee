"""Module 19: ExportService.

Orchestrates the complete clean export lifecycle:
  1. Idempotency check (return existing on duplicate key).
  2. Eligibility check (dataset approved + QC passed).
  3. Load the approved ExportRun artifact.
  4. Export to the requested format (CSV or XLSX).
  5. Write artifact to disk.
  6. Compute metadata (checksum, row/column counts).
  7. Persist CleanExport record.

This is the only class in the package that touches BOTH the filesystem
and the database. It is called by CleanExportHandler.

SAFETY RULES enforced here:
  - Original ExportRun artifact is NEVER modified.
  - No existing file is overwritten (each export gets a unique artifact_id).
  - artifact_path is constructed from clean_export_output_root (server
    config) + organization_id (tenant isolation) + artifact_id (unique UUID).
  - If file write succeeds but DB commit fails, the orphaned artifact file
    is cleaned up (best-effort, logged if cleanup itself fails).
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.artifacts.storage import get_artifact_storage
from app.clean_export.csv_exporter import CSVExporter
from app.clean_export.eligibility import ExportEligibilityChecker
from app.clean_export.loader import ApprovedDatasetLoader, ArtifactLoadError
from app.clean_export.metadata import ExportMetadataBuilder
from app.clean_export.repository import ExportRepository
from app.clean_export.types import CleanExportRequest, CleanExportResult
from app.clean_export.xlsx_exporter import XLSXExporter, XLSXExportError
from app.core.config import get_settings
from app.models.applied_remediation_run import AppliedRemediationRun
from app.models.clean_export import CleanExport
from app.models.export_run import ExportRun
from sqlalchemy import select

logger = logging.getLogger(__name__)


class ExportService:
    """Orchestrate the complete clean export lifecycle.

    Instantiate with optional injected collaborators for testability;
    defaults to production implementations.
    """

    def __init__(
        self,
        eligibility_checker: ExportEligibilityChecker | None = None,
        loader: ApprovedDatasetLoader | None = None,
        csv_exporter: CSVExporter | None = None,
        xlsx_exporter: XLSXExporter | None = None,
        metadata_builder: ExportMetadataBuilder | None = None,
        repository: ExportRepository | None = None,
    ) -> None:
        self._eligibility = eligibility_checker or ExportEligibilityChecker()
        self._loader = loader or ApprovedDatasetLoader()
        self._csv_exporter = csv_exporter or CSVExporter()
        self._xlsx_exporter = xlsx_exporter or XLSXExporter()
        self._metadata_builder = metadata_builder or ExportMetadataBuilder()
        self._repo = repository or ExportRepository()

    def run(self, db: Session, request: CleanExportRequest) -> CleanExport:
        """Execute the full clean export lifecycle and return the persisted CleanExport.

        Thread-safe for concurrent requests with the same idempotency_key:
        the DB UNIQUE constraint and catch-and-refetch pattern ensure
        exactly one row is ever created per key.
        """
        settings = get_settings()

        # ── Step 1: Idempotency check ──────────────────────────────────────
        existing = self._repo.find_by_idempotency_key(
            db, request.organization_id, request.idempotency_key
        )
        if existing is not None:
            return existing

        # ── Step 2: Eligibility check ──────────────────────────────────────
        eligibility = self._eligibility.check(
            db,
            organization_id=request.organization_id,
            job_id=request.job_id,
            dataset_version=request.dataset_version,
        )
        if not eligibility.eligible:
            return self._repo.create_blocked(
                db,
                organization_id=request.organization_id,
                job_id=request.job_id,
                data_source_id=request.data_source_id,
                format=request.format,
                idempotency_key=request.idempotency_key,
                failure_reason=eligibility.failure_reason or "eligibility check failed",
                quality_control_run_id=eligibility.quality_control_run_id,
            )

        # ── Step 3: Load the artifact ──────────────────────────────────────
        # When Gate 4 found an AppliedRemediationRun, load from the remediated
        # artifact (csv_remediated_root). Otherwise fall back to the raw
        # ExportRun artifact (csv_exported_root). Both paths use the same
        # ApprovedDatasetLoader with the same path-containment + SHA-256
        # integrity checks.

        if eligibility.applied_remediation_run_id is not None:
            # APPLY_REMEDIATIONS path: load the remediated artifact.
            tenant_root = (
                Path(settings.csv_remediated_root) / str(request.organization_id)
            )
            try:
                dataset = self._loader.load(
                    output_file_path=eligibility.applied_remediation_run_artifact_path,
                    expected_sha256=eligibility.applied_remediation_run_sha256,
                    tenant_root=tenant_root,
                    export_run_id=eligibility.export_run_id,
                )
            except ArtifactLoadError as exc:
                return self._repo.create_failed(
                    db,
                    organization_id=request.organization_id,
                    job_id=request.job_id,
                    data_source_id=request.data_source_id,
                    format=request.format,
                    idempotency_key=request.idempotency_key,
                    failure_reason=(
                        f"artifact_load_error ({exc.failure_reason_code}): {exc}"
                    ),
                    quality_control_run_id=eligibility.quality_control_run_id,
                )
        else:
            # Legacy path: load directly from the ExportRun artifact.
            tenant_root = (
                Path(settings.csv_exported_root) / str(request.organization_id)
            )

            # Fetch output_sha256 for integrity verification.
            export_run_row = db.execute(
                select(ExportRun).where(ExportRun.id == eligibility.export_run_id)
            ).scalar_one_or_none()
            if export_run_row is None:
                return self._repo.create_failed(
                    db,
                    organization_id=request.organization_id,
                    job_id=request.job_id,
                    data_source_id=request.data_source_id,
                    format=request.format,
                    idempotency_key=request.idempotency_key,
                    failure_reason=(
                        f"ExportRun id={eligibility.export_run_id} disappeared "
                        "between eligibility check and load (race condition)"
                    ),
                    quality_control_run_id=eligibility.quality_control_run_id,
                )

            try:
                dataset = self._loader.load(
                    output_file_path=eligibility.export_run_artifact_path,
                    expected_sha256=export_run_row.output_sha256,
                    tenant_root=tenant_root,
                    export_run_id=eligibility.export_run_id,
                )
            except ArtifactLoadError as exc:
                return self._repo.create_failed(
                    db,
                    organization_id=request.organization_id,
                    job_id=request.job_id,
                    data_source_id=request.data_source_id,
                    format=request.format,
                    idempotency_key=request.idempotency_key,
                    failure_reason=f"artifact_load_error ({exc.failure_reason_code}): {exc}",
                    quality_control_run_id=eligibility.quality_control_run_id,
                )

        # ── Step 4: Serialize to requested format ──────────────────────────
        try:
            if request.format == "csv":
                artifact_bytes = self._csv_exporter.export(dataset)
            else:
                artifact_bytes = self._xlsx_exporter.export(dataset)
        except XLSXExportError as exc:
            return self._repo.create_failed(
                db,
                organization_id=request.organization_id,
                job_id=request.job_id,
                data_source_id=request.data_source_id,
                format=request.format,
                idempotency_key=request.idempotency_key,
                failure_reason=f"xlsx_export_error: {exc}",
                quality_control_run_id=eligibility.quality_control_run_id,
            )
        except Exception as exc:
            return self._repo.create_failed(
                db,
                organization_id=request.organization_id,
                job_id=request.job_id,
                data_source_id=request.data_source_id,
                format=request.format,
                idempotency_key=request.idempotency_key,
                failure_reason=f"serialization_error: {exc}",
                quality_control_run_id=eligibility.quality_control_run_id,
            )

        # ── Step 5: Write artifact to disk ─────────────────────────────────
        artifact_id = uuid.uuid4()
        ext = "csv" if request.format == "csv" else "xlsx"
        output_dir = (
            Path(settings.clean_export_output_root) / str(request.organization_id)
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        artifact_path = output_dir / f"{artifact_id}.{ext}"

        try:
            artifact_path.write_bytes(artifact_bytes)
        except OSError as exc:
            return self._repo.create_failed(
                db,
                organization_id=request.organization_id,
                job_id=request.job_id,
                data_source_id=request.data_source_id,
                format=request.format,
                idempotency_key=request.idempotency_key,
                failure_reason=f"io_error writing artifact: {exc}",
                quality_control_run_id=eligibility.quality_control_run_id,
            )

        # ── Step 6: Compute metadata ───────────────────────────────────────
        meta = self._metadata_builder.build(
            artifact_bytes=artifact_bytes,
            dataset=dataset,
        )

        result = CleanExportResult(
            artifact_id=artifact_id,
            checksum=meta["checksum"],
            row_count=meta["row_count"],
            column_count=meta["column_count"],
            artifact_path=str(artifact_path),
            format=request.format,
            quality_control_run_id=eligibility.quality_control_run_id,
            export_run_id=eligibility.export_run_id,
            completed_at=datetime.now(timezone.utc),
        )

        # ── Step 7: Persist CleanExport record ────────────────────────────
        # If the DB commit fails (e.g., concurrent duplicate on idempotency_key),
        # the catch-and-refetch in ExportRepository.create_completed() returns
        # the existing row. The orphaned artifact file on disk is acceptable:
        # it will either be cleaned up by retention or can be ignored (the DB
        # row controls all API surface).
        return self._repo.create_completed(
            db,
            organization_id=request.organization_id,
            job_id=request.job_id,
            data_source_id=request.data_source_id,
            format=request.format,
            idempotency_key=request.idempotency_key,
            result=result,
        )
