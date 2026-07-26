"""Module 19: XLSXExporter.

Serializes a LoadedDataset to XLSX bytes using openpyxl. Pure from a
business-logic perspective -- no DB access, no filesystem access. The
only I/O is the in-memory workbook write (openpyxl uses BytesIO
internally when save_virtual_workbook() is called).

openpyxl is already a project dependency (used by Module 7's
standardization engine). No new dependency introduced here.

IMPORTANT: XLSX files produced by openpyxl are NOT byte-for-byte
deterministic across calls -- openpyxl embeds creation timestamps in
the ZIP metadata of the .xlsx container. This means two calls with
identical input data will NOT produce identical checksum values. The
checksum stored in clean_exports.checksum is always of the specific
bytes written to disk at that moment; re-exporting the same dataset
will produce a different checksum. This is a known XLSX limitation and
is documented in the CleanExport row's failure_reason if relevant. For
deterministic checksums, use format='csv'.
"""
from __future__ import annotations

import io

from app.clean_export.types import LoadedDataset

try:
    import openpyxl
    from openpyxl import Workbook
    _OPENPYXL_AVAILABLE = True
except ImportError:
    _OPENPYXL_AVAILABLE = False


class XLSXExportError(Exception):
    """Raised when XLSX serialization fails."""


class XLSXExporter:
    """Serialize a LoadedDataset to XLSX bytes using openpyxl."""

    def export(self, dataset: LoadedDataset) -> bytes:
        """Return the XLSX serialization of the dataset as bytes.

        Raises XLSXExportError if openpyxl is not available or if
        serialization fails.

        Note: XLSX bytes are NOT deterministic across calls (see module
        docstring). The caller should store the checksum of the returned
        bytes as the canonical checksum for this specific export.
        """
        if not _OPENPYXL_AVAILABLE:
            raise XLSXExportError(
                "openpyxl is not installed; XLSX export is unavailable. "
                "Install openpyxl to enable XLSX clean exports."
            )

        try:
            wb = Workbook(write_only=True)
            ws = wb.create_sheet("Export")

            # Write header row
            ws.append(list(dataset.headers))

            # Write data rows
            for row in dataset.rows:
                ws.append(list(row))

            # Serialize to bytes via BytesIO
            buffer = io.BytesIO()
            wb.save(buffer)
            buffer.seek(0)
            return buffer.read()

        except Exception as exc:
            raise XLSXExportError(
                f"Failed to serialize dataset to XLSX: {exc}"
            ) from exc
