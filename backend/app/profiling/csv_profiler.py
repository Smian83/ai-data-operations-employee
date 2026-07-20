"""Deterministic, read-only CSV profiling.

The profiler never mutates source rows. It calculates bounded quality metadata
from a :class:`LoadedCsv` and returns an immutable :class:`ProfileResult`.
"""
from __future__ import annotations

import re
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Iterable

from app.profiling.types import CsvLimits, LoadedCsv, ProfileResult

_INTEGER_RE = re.compile(r"^[+-]?\d+$")
_BOOLEAN_VALUES = {"true", "false", "yes", "no", "y", "n"}
_TYPE_PRIORITY = ("boolean", "integer", "decimal", "datetime", "date", "string")


def _value_type(value: str) -> str:
    stripped = value.strip()
    if not stripped:
        return "null"
    lowered = stripped.casefold()
    if lowered in _BOOLEAN_VALUES:
        return "boolean"
    if _INTEGER_RE.fullmatch(stripped):
        return "integer"
    try:
        Decimal(stripped)
    except InvalidOperation:
        pass
    else:
        return "decimal"
    try:
        parsed_datetime = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
    except ValueError:
        parsed_datetime = None
    if parsed_datetime is not None and ("T" in stripped or " " in stripped):
        return "datetime"
    try:
        date.fromisoformat(stripped)
    except ValueError:
        return "string"
    return "date"


def _dominant_type(type_counts: Counter[str]) -> tuple[str, str | None]:
    """Return (reported type, dominant concrete type).

    A column is reported as ``mixed`` when no concrete type accounts for at
    least 80% of non-null values or when the top two types tie. The dominant
    concrete type is still returned for inconsistency accounting.
    """
    if not type_counts:
        return "null", None
    ranked = sorted(
        type_counts.items(),
        key=lambda item: (-item[1], _TYPE_PRIORITY.index(item[0])),
    )
    dominant, count = ranked[0]
    total = sum(type_counts.values())
    tied = len(ranked) > 1 and ranked[1][1] == count
    if tied or count / total < 0.8:
        return "mixed", dominant
    return dominant, dominant


def _bounded_unique(values: Iterable[str], limit: int) -> list[str]:
    retained: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        retained.append(value)
        if len(retained) >= limit:
            break
    return retained


def profile_csv(loaded: LoadedCsv, limits: CsvLimits) -> ProfileResult:
    row_count = len(loaded.rows)
    column_count = len(loaded.headers)
    normalized_rows = [tuple(cell.strip() for cell in row) for row in loaded.rows]
    duplicate_row_count = row_count - len(set(normalized_rows))

    column_profiles: list[dict] = []
    structural_issues = [dict(issue) for issue in loaded.structural_issues]
    missing_value_total = 0

    for index, name in enumerate(loaded.headers):
        values = [row[index].strip() for row in loaded.rows]
        non_null_values = [value for value in values if value]
        missing_count = row_count - len(non_null_values)
        missing_value_total += missing_count

        value_types = [_value_type(value) for value in non_null_values]
        type_counts: Counter[str] = Counter(value_types)
        inferred_type, dominant_type = _dominant_type(type_counts)
        inconsistent_count = (
            sum(count for value_type, count in type_counts.items() if value_type != dominant_type)
            if dominant_type is not None
            else 0
        )
        if inferred_type == "mixed":
            inconsistent_count = len(non_null_values)
        if inconsistent_count:
            structural_issues.append(
                {
                    "type": "inconsistent_column_type",
                    "column_index": index,
                    "column_name": name,
                    "inferred_type": inferred_type,
                    "dominant_type": dominant_type,
                    "inconsistent_value_count": inconsistent_count,
                }
            )

        distinct_values = set(non_null_values)
        samples = _bounded_unique(non_null_values, limits.max_sample_values)
        retained_distinct = _bounded_unique(non_null_values, limits.max_distinct_values)
        column_profiles.append(
            {
                "name": name,
                "position": index,
                "inferred_type": inferred_type,
                "non_null_count": len(non_null_values),
                "missing_count": missing_count,
                "missing_percentage": round((missing_count / row_count * 100), 4)
                if row_count
                else 0.0,
                "distinct_count": len(distinct_values),
                "sample_values": samples,
                "retained_distinct_values": retained_distinct,
                "distinct_values_truncated": len(distinct_values) > len(retained_distinct),
                "inconsistent_value_count": inconsistent_count,
                "observed_type_counts": dict(sorted(type_counts.items())),
            }
        )

    return ProfileResult(
        source_filename=loaded.path.name,
        source_size_bytes=loaded.source_size_bytes,
        source_sha256=loaded.source_sha256,
        detected_encoding=loaded.detected_encoding,
        delimiter=loaded.delimiter,
        row_count=row_count,
        column_count=column_count,
        duplicate_row_count=duplicate_row_count,
        missing_value_total=missing_value_total,
        column_profiles=column_profiles,
        structural_issues=structural_issues,
        limits_applied=limits.as_dict(),
    )
