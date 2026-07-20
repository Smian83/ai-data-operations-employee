"""Module 5 deterministic profiling tests."""
from pathlib import Path

from app.profiling.csv_profiler import profile_csv
from app.profiling.types import CsvLimits, LoadedCsv


def _limits() -> CsvLimits:
    return CsvLimits(
        max_file_size_bytes=1024,
        max_rows=100,
        max_columns=20,
        max_cell_length=100,
        max_distinct_values=2,
        max_sample_values=2,
    )


def test_profile_csv_calculates_quality_metrics() -> None:
    loaded = LoadedCsv(
        path=Path("customers.csv"),
        source_size_bytes=123,
        source_sha256="a" * 64,
        detected_encoding="utf-8",
        delimiter=",",
        headers=["id", "active", "joined", "amount", "note"],
        rows=[
            ["1", "true", "2026-01-01", "10.50", "alpha"],
            ["2", "false", "2026-01-02", "20", "beta"],
            ["2", "false", "2026-01-02", "20", "beta"],
            ["", "yes", "not-a-date", "bad", "gamma"],
        ],
        structural_issues=[],
    )

    result = profile_csv(loaded, _limits())

    assert result.row_count == 4
    assert result.column_count == 5
    assert result.duplicate_row_count == 1
    assert result.missing_value_total == 1

    by_name = {column["name"]: column for column in result.column_profiles}
    assert by_name["id"]["inferred_type"] == "integer"
    assert by_name["id"]["missing_count"] == 1
    assert by_name["active"]["inferred_type"] == "boolean"
    assert by_name["joined"]["inferred_type"] == "mixed"
    assert by_name["amount"]["inferred_type"] == "mixed"
    assert by_name["note"]["distinct_count"] == 3
    assert by_name["note"]["distinct_values_truncated"] is True
    assert by_name["note"]["sample_values"] == ["alpha", "beta"]

    inconsistent_columns = {
        issue["column_name"]
        for issue in result.structural_issues
        if issue["type"] == "inconsistent_column_type"
    }
    assert inconsistent_columns == {"joined", "amount"}


def test_profile_csv_reports_null_column_and_zero_row_percentages() -> None:
    loaded = LoadedCsv(
        path=Path("empty.csv"),
        source_size_bytes=5,
        source_sha256="b" * 64,
        detected_encoding="utf-8",
        delimiter=",",
        headers=["only"],
        rows=[],
        structural_issues=[],
    )

    result = profile_csv(loaded, _limits())
    column = result.column_profiles[0]
    assert column["inferred_type"] == "null"
    assert column["missing_percentage"] == 0.0
    assert column["distinct_count"] == 0
