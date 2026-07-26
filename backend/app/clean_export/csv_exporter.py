"""Module 19: CSVExporter.

Serializes a LoadedDataset to UTF-8 CSV bytes. Pure function -- no I/O,
no DB access, no side effects. Mirrors _serialize_csv() in
app.worker.handlers.export (same reuse-by-copy precedent established
there: a small deterministic helper intentionally duplicated rather than
factored into a shared module).
"""
from __future__ import annotations

import csv
from io import StringIO

from app.clean_export.types import LoadedDataset

CLEAN_EXPORT_CSV_FORMAT_VERSION: int = 1
"""Format version for Module 19 clean export CSVs. Increment when the
serialization format changes in a backward-incompatible way."""


def serialize_to_csv(dataset: LoadedDataset) -> bytes:
    """Serialize headers + rows to UTF-8 CSV bytes.

    Deterministic: identical headers and rows always produce identical
    bytes (no timestamp, no metadata embedded in the file).
    """
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(dataset.headers)
    writer.writerows(dataset.rows)
    return buffer.getvalue().encode("utf-8")


class CSVExporter:
    """Serialize a LoadedDataset to CSV bytes."""

    def export(self, dataset: LoadedDataset) -> bytes:
        """Return the CSV serialization of the dataset.

        Guaranteed deterministic for identical input.
        """
        return serialize_to_csv(dataset)
