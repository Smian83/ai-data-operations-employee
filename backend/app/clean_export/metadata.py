"""Module 19: ExportMetadataBuilder.

Computes artifact metadata (SHA-256 checksum, row count, column count)
from artifact bytes + the LoadedDataset. Pure function -- no I/O, no
DB access.
"""
from __future__ import annotations

import hashlib

from app.clean_export.types import LoadedDataset


def compute_checksum(artifact_bytes: bytes) -> str:
    """Return the SHA-256 hex digest of artifact_bytes."""
    return hashlib.sha256(artifact_bytes).hexdigest()


class ExportMetadataBuilder:
    """Compute metadata from artifact bytes and the loaded dataset."""

    def build(
        self,
        *,
        artifact_bytes: bytes,
        dataset: LoadedDataset,
    ) -> dict:
        """Return a metadata dict with checksum, row_count, column_count.

        row_count: number of data rows (excluding header).
        column_count: number of columns (from header).
        checksum: SHA-256 hex digest of artifact_bytes.
        """
        return {
            "checksum": compute_checksum(artifact_bytes),
            "row_count": len(dataset.rows),
            "column_count": len(dataset.headers),
        }
