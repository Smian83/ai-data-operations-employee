"""Module 5 safe CSV loading tests."""
import codecs
from pathlib import Path

import pytest

from app.profiling.csv_loader import CsvLoadError, load_csv, resolve_source_path
from app.profiling.types import CsvLimits


@pytest.fixture
def limits() -> CsvLimits:
    return CsvLimits(
        max_file_size_bytes=1024,
        max_rows=3,
        max_columns=3,
        max_cell_length=20,
        max_distinct_values=5,
        max_sample_values=3,
    )


def test_resolve_source_path_rejects_escape(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(CsvLoadError, match="escapes"):
        resolve_source_path(root, "../outside.csv")


def test_load_csv_is_read_only_and_profiles_structure(tmp_path: Path, limits: CsvLimits) -> None:
    source = tmp_path / "source.csv"
    original = "name,name,\nage,20\nshort\nextra,field,ignored,overflow\n"
    source.write_text(original, encoding="utf-8")

    loaded = load_csv(source, limits)

    assert source.read_text(encoding="utf-8") == original
    assert loaded.detected_encoding == "utf-8"
    assert loaded.headers == ["name", "name", ""]
    assert loaded.rows == [["age", "20", ""], ["short", "", ""], ["extra", "field", "ignored"]]
    issue_types = {issue["type"] for issue in loaded.structural_issues}
    assert issue_types == {
        "duplicate_header",
        "blank_header",
        "too_few_fields",
        "too_many_fields",
    }


def test_load_csv_detects_utf8_bom(tmp_path: Path, limits: CsvLimits) -> None:
    source = tmp_path / "bom.csv"
    source.write_bytes(codecs.BOM_UTF8 + b"name\nvalue\n")
    loaded = load_csv(source, limits)
    assert loaded.detected_encoding == "utf-8-sig"
    assert loaded.headers == ["name"]


def test_load_csv_enforces_row_limit(tmp_path: Path, limits: CsvLimits) -> None:
    source = tmp_path / "rows.csv"
    source.write_text("name\na\nb\nc\nd\n", encoding="utf-8")
    with pytest.raises(CsvLoadError, match="row limit"):
        load_csv(source, limits)


def test_load_csv_rejects_invalid_encoding(tmp_path: Path, limits: CsvLimits) -> None:
    source = tmp_path / "bad.csv"
    source.write_bytes(b"name\n\xff\n")
    with pytest.raises(CsvLoadError, match="valid UTF-8"):
        load_csv(source, limits)
