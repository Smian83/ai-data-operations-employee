"""Module 19: ApprovedDatasetLoader.

Loads the approved ExportRun artifact CSV into an in-memory LoadedDataset.
Uses the same path-containment and integrity-verification approach as
app.artifacts.download (Module 10):

  1. resolve_artifact_path() confirms the path is inside the tenant root.
  2. open_verified_artifact() does a two-pass read: hash then stream.

The Module 9 ExportRun artifact is NEVER opened for writing. This module
is strictly read-only.
"""
from __future__ import annotations

import csv
import io
import uuid
from pathlib import Path

from app.artifacts.download import (
    ArtifactIntegrityError,
    ArtifactMissingError,
    ArtifactPathError,
    open_verified_artifact,
    resolve_artifact_path,
)
from app.clean_export.types import LoadedDataset


class ArtifactLoadError(Exception):
    """Raised when the approved ExportRun artifact cannot be loaded.

    failure_reason_code distinguishes the cause:
      'path_containment_violation' -- path escapes tenant root
      'artifact_missing'           -- file not found or not readable
      'artifact_integrity'         -- SHA-256 mismatch
      'csv_parse_error'            -- file cannot be parsed as CSV
    """
    def __init__(self, failure_reason_code: str, message: str) -> None:
        super().__init__(message)
        self.failure_reason_code = failure_reason_code


class ApprovedDatasetLoader:
    """Load the approved ExportRun artifact CSV into memory.

    Accepts the export_run's output_file_path (an absolute server-written
    path), the expected SHA-256 checksum, the tenant_root to enforce
    containment, and the export_run_id for audit.

    Never writes anything. Uses the verified open_verified_artifact()
    helper which guarantees hash verification before any bytes are returned.
    """

    def load(
        self,
        *,
        output_file_path: str,
        expected_sha256: str,
        tenant_root: Path,
        export_run_id: uuid.UUID,
    ) -> LoadedDataset:
        """Load and parse the artifact CSV.

        Raises ArtifactLoadError on any failure (path escape, missing
        file, hash mismatch, or invalid CSV).
        """
        # Path containment check (mirrors Module 10 defense-in-depth).
        try:
            resolved = resolve_artifact_path(tenant_root, output_file_path)
        except ArtifactPathError as exc:
            raise ArtifactLoadError(
                "path_containment_violation",
                f"ExportRun artifact path escapes tenant root: {exc}",
            ) from exc

        # Open + verify integrity in one pass, then read.
        try:
            fileobj = open_verified_artifact(resolved, expected_sha256)
        except ArtifactMissingError as exc:
            raise ArtifactLoadError(
                "artifact_missing",
                f"ExportRun artifact not found or unreadable "
                f"(export_run_id={export_run_id}, "
                f"reason={exc.failure_reason_code}): {exc}",
            ) from exc
        except ArtifactIntegrityError as exc:
            raise ArtifactLoadError(
                "artifact_integrity",
                f"ExportRun artifact SHA-256 mismatch "
                f"(export_run_id={export_run_id}): {exc}",
            ) from exc

        # Read all bytes from the verified, open descriptor.
        try:
            raw_bytes = fileobj.read()
        finally:
            fileobj.close()

        # Parse the CSV. ExportRun artifacts are always UTF-8 encoded
        # (produced by _serialize_csv() in app.worker.handlers.export).
        try:
            text = raw_bytes.decode("utf-8")
            reader = csv.reader(io.StringIO(text))
            all_rows = list(reader)
        except (UnicodeDecodeError, csv.Error) as exc:
            raise ArtifactLoadError(
                "csv_parse_error",
                f"ExportRun artifact is not valid UTF-8 CSV "
                f"(export_run_id={export_run_id}): {exc}",
            ) from exc

        if not all_rows:
            raise ArtifactLoadError(
                "csv_parse_error",
                f"ExportRun artifact is empty (export_run_id={export_run_id})",
            )

        headers = tuple(all_rows[0])
        rows = tuple(tuple(row) for row in all_rows[1:])

        return LoadedDataset(
            headers=headers,
            rows=rows,
            source_export_run_id=export_run_id,
            source_sha256=expected_sha256,
        )
