"""Safe, bounded and strictly read-only CSV loading."""
import csv
import hashlib
import io
from pathlib import Path

from app.profiling.types import CsvLimits, LoadedCsv


class CsvLoadError(ValueError):
    """Raised when a CSV source is unsafe, malformed or exceeds configured limits."""


def resolve_source_path(input_root: Path, configured_path: str) -> Path:
    if not configured_path or not configured_path.strip():
        raise CsvLoadError("CSV data source requires connection_metadata.file_path")
    candidate = Path(configured_path)
    if candidate.is_absolute():
        raise CsvLoadError("Absolute CSV paths are not allowed")
    root = input_root.resolve()
    resolved = (root / candidate).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise CsvLoadError("CSV path escapes the configured input root") from exc
    if not resolved.is_file():
        raise CsvLoadError("CSV source file was not found")
    return resolved


def _decode(raw: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "utf-8"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise CsvLoadError("CSV source is not valid UTF-8")


def load_csv(path: Path, limits: CsvLimits) -> LoadedCsv:
    size = path.stat().st_size
    if size > limits.max_file_size_bytes:
        raise CsvLoadError("CSV file exceeds the configured size limit")

    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    text, encoding = _decode(raw)
    if not text.strip():
        raise CsvLoadError("CSV source is empty")

    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel

    reader = csv.reader(io.StringIO(text, newline=""), dialect)
    try:
        headers = next(reader)
    except StopIteration as exc:
        raise CsvLoadError("CSV source does not contain a header row") from exc
    except csv.Error as exc:
        raise CsvLoadError("CSV header could not be parsed") from exc

    if len(headers) > limits.max_columns:
        raise CsvLoadError("CSV exceeds the configured column limit")
    if not headers:
        raise CsvLoadError("CSV header row is empty")

    issues: list[dict] = []
    normalized_headers = [header.strip() for header in headers]
    seen: dict[str, int] = {}
    for index, header in enumerate(normalized_headers):
        if not header:
            issues.append({"type": "blank_header", "column_index": index})
        key = header.casefold()
        if key in seen:
            issues.append(
                {"type": "duplicate_header", "column_index": index, "first_column_index": seen[key]}
            )
        else:
            seen[key] = index

    rows: list[list[str]] = []
    try:
        for row_number, row in enumerate(reader, start=2):
            if len(rows) >= limits.max_rows:
                raise CsvLoadError("CSV exceeds the configured row limit")
            if len(row) < len(headers):
                issues.append(
                    {"type": "too_few_fields", "row_number": row_number, "field_count": len(row)}
                )
                row = row + [""] * (len(headers) - len(row))
            elif len(row) > len(headers):
                issues.append(
                    {"type": "too_many_fields", "row_number": row_number, "field_count": len(row)}
                )
                row = row[: len(headers)]
            for column_index, value in enumerate(row):
                if len(value) > limits.max_cell_length:
                    raise CsvLoadError(
                        f"CSV cell exceeds the configured length limit at row {row_number}, column {column_index + 1}"
                    )
            rows.append(row)
    except csv.Error as exc:
        raise CsvLoadError("CSV body could not be parsed") from exc

    return LoadedCsv(
        path=path,
        source_size_bytes=size,
        source_sha256=digest,
        detected_encoding=encoding,
        delimiter=dialect.delimiter,
        headers=normalized_headers,
        rows=rows,
        structural_issues=issues,
    )
