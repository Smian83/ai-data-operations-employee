"""Module 19: Clean Export Engine package.

Exports ONLY approved, validated, and quality-controlled datasets.

Public surface:
  CleanExportEngine       -- pure, orchestration-free engine (I/O-free)
  ExportEligibilityChecker -- verifies a dataset is eligible for export
  ApprovedDatasetLoader   -- loads the approved ExportRun artifact CSV
  CSVExporter             -- serializes loaded data to CSV bytes
  XLSXExporter            -- serializes loaded data to XLSX bytes
  ExportMetadataBuilder   -- computes checksum, row/column counts
  ExportRepository        -- CleanExport DB access (read + write)
  ExportService           -- orchestrates the full export lifecycle

All pure functions in this package (eligibility checks, exporters,
metadata builder) are deliberately I/O-free and take no SQLAlchemy
sessions. Only ExportRepository and ExportService touch the database
or filesystem. CleanExportHandler (in app.worker.handlers.clean_export)
is the sole impure orchestrator.
"""
from app.clean_export.types import (
    CleanExportRequest,
    CleanExportResult,
    EligibilityResult,
    LoadedDataset,
)
from app.clean_export.eligibility import ExportEligibilityChecker
from app.clean_export.loader import ApprovedDatasetLoader
from app.clean_export.csv_exporter import CSVExporter
from app.clean_export.xlsx_exporter import XLSXExporter
from app.clean_export.metadata import ExportMetadataBuilder
from app.clean_export.repository import ExportRepository
from app.clean_export.service import ExportService

__all__ = [
    "CleanExportRequest",
    "CleanExportResult",
    "EligibilityResult",
    "LoadedDataset",
    "ExportEligibilityChecker",
    "ApprovedDatasetLoader",
    "CSVExporter",
    "XLSXExporter",
    "ExportMetadataBuilder",
    "ExportRepository",
    "ExportService",
]
