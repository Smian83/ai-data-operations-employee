"""Internal immutable value objects shared by the CSV loader and profiler."""
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CsvLimits:
    max_file_size_bytes: int
    max_rows: int
    max_columns: int
    max_cell_length: int
    max_distinct_values: int
    max_sample_values: int

    def as_dict(self) -> dict[str, int]:
        return {
            "max_file_size_bytes": self.max_file_size_bytes,
            "max_rows": self.max_rows,
            "max_columns": self.max_columns,
            "max_cell_length": self.max_cell_length,
            "max_distinct_values": self.max_distinct_values,
            "max_sample_values": self.max_sample_values,
        }


@dataclass(frozen=True)
class LoadedCsv:
    path: Path
    source_size_bytes: int
    source_sha256: str
    detected_encoding: str
    delimiter: str
    headers: list[str]
    rows: list[list[str]]
    structural_issues: list[dict[str, Any]]


@dataclass(frozen=True)
class ProfileResult:
    source_filename: str
    source_size_bytes: int
    source_sha256: str
    detected_encoding: str
    delimiter: str
    row_count: int
    column_count: int
    duplicate_row_count: int
    missing_value_total: int
    column_profiles: list[dict[str, Any]]
    structural_issues: list[dict[str, Any]]
    limits_applied: dict[str, int]
