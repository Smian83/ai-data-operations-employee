"""
Module 6: the fixed, ordered set of deterministic cleaning rules described
in docs/module-6-data-cleaning-engine-design.md Section 9. Every function
here is pure -- no I/O, no randomness, no wall-clock or locale dependence
-- so that the same input always produces the same output, which is this
module's determinism acceptance criterion. Confidence values are fixed per
rule, never computed from the data.

Note on structural repair (Section 9, item 1): app.profiling.csv_loader's
load_csv already pads short rows and truncates long rows to a uniform
length before CleaningHandler ever sees the data (CleaningHandler reuses
that exact function -- see the design's Section 8). Rows arriving here are
therefore already structurally uniform; there is no separate repair rule
in this module, since Module 5's loader already performs it and re-doing
it here would be dead code, not a missing feature.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

_BLANK_EQUIVALENTS = {"null", "n/a", "na", "-", "none"}
_BOOLEAN_TRUE = {"true", "yes", "y", "1"}
_BOOLEAN_FALSE = {"false", "no", "n", "0"}
_INTEGER_RE = re.compile(r"^[+-]?\d+$")
_WHITESPACE_RE = re.compile(r"\s+")

RULE_TRIM_WHITESPACE = "trim_whitespace"
RULE_NORMALIZE_BLANK = "normalize_blank"
RULE_COERCE_INTEGER = "coerce_integer"
RULE_COERCE_DECIMAL = "coerce_decimal"
RULE_COERCE_BOOLEAN = "coerce_boolean"
RULE_NORMALIZE_DATE = "normalize_date_format"

# Fixed per-rule confidence -- mechanical, unambiguous repairs (whitespace)
# are 1.0; date reparsing, the least certain coercion, is the lowest.
RULE_CONFIDENCE: dict[str, float] = {
    RULE_TRIM_WHITESPACE: 1.0,
    RULE_NORMALIZE_BLANK: 0.9,
    RULE_COERCE_INTEGER: 0.85,
    RULE_COERCE_DECIMAL: 0.8,
    RULE_COERCE_BOOLEAN: 0.9,
    RULE_NORMALIZE_DATE: 0.7,
}

RULE_REASONS: dict[str, str] = {
    RULE_TRIM_WHITESPACE: "Leading/trailing or repeated internal whitespace removed.",
    RULE_NORMALIZE_BLANK: "Blank-equivalent value normalized to a missing value.",
    RULE_COERCE_INTEGER: "Value coerced to the column's canonical integer form.",
    RULE_COERCE_DECIMAL: "Value coerced to the column's canonical decimal form.",
    RULE_COERCE_BOOLEAN: "Value coerced to the column's canonical boolean form.",
    RULE_NORMALIZE_DATE: "Value reparsed into ISO-8601 date/datetime form.",
}


def trim_whitespace(value: str) -> tuple[str, str | None]:
    """Trim leading/trailing whitespace and collapse repeated internal
    whitespace to a single space. Returns (result, rule_name_or_None)."""
    collapsed = _WHITESPACE_RE.sub(" ", value.strip())
    if collapsed == value:
        return value, None
    return collapsed, RULE_TRIM_WHITESPACE


def normalize_blank(value: str) -> tuple[str, str | None]:
    """Canonicalize blank-equivalents ('N/A', '-', 'null', 'none') to the
    single missing-value representation used throughout this project: the
    empty string (see DataProfile's own missing_count/non_null_count,
    which already treat '' as the sole missing marker)."""
    if value == "":
        return value, None
    if value.casefold() in _BLANK_EQUIVALENTS:
        return "", RULE_NORMALIZE_BLANK
    return value, None


def coerce_type(value: str, inferred_type: str) -> tuple[str, str | None]:
    """Coerce a non-conforming value toward inferred_type's canonical
    string form. inferred_type is the column's DataProfile-reported
    dominant type -- 'integer'/'decimal'/'boolean'/'date'/'datetime'. Any
    other value ('string', 'mixed', 'null') is left untouched: this module
    only coerces columns whose type is already concrete and established,
    exactly as Section 9 specifies."""
    if value == "":
        return value, None
    if inferred_type == "integer":
        return _coerce_integer(value)
    if inferred_type == "decimal":
        return _coerce_decimal(value)
    if inferred_type == "boolean":
        return _coerce_boolean(value)
    if inferred_type in ("date", "datetime"):
        return _coerce_date(value)
    return value, None


def _coerce_integer(value: str) -> tuple[str, str | None]:
    stripped = value.strip()
    if _INTEGER_RE.fullmatch(stripped):
        return (value, None) if stripped == value else (stripped, RULE_COERCE_INTEGER)
    try:
        as_decimal = Decimal(stripped)
    except InvalidOperation:
        return value, None
    if as_decimal.is_finite() and as_decimal == as_decimal.to_integral_value():
        canonical = str(int(as_decimal))
        return canonical, RULE_COERCE_INTEGER
    return value, None


def _coerce_decimal(value: str) -> tuple[str, str | None]:
    stripped = value.strip()
    try:
        as_decimal = Decimal(stripped)
    except InvalidOperation:
        return value, None
    if not as_decimal.is_finite():
        return value, None
    canonical = format(as_decimal.normalize(), "f")
    if canonical == value:
        return value, None
    return canonical, RULE_COERCE_DECIMAL


def _coerce_boolean(value: str) -> tuple[str, str | None]:
    lowered = value.strip().casefold()
    if lowered in _BOOLEAN_TRUE:
        return ("true", None) if value == "true" else ("true", RULE_COERCE_BOOLEAN)
    if lowered in _BOOLEAN_FALSE:
        return ("false", None) if value == "false" else ("false", RULE_COERCE_BOOLEAN)
    return value, None


def _coerce_date(value: str) -> tuple[str, str | None]:
    stripped = value.strip()
    canonical: str | None = None
    try:
        canonical = datetime.fromisoformat(stripped.replace("Z", "+00:00")).isoformat()
    except ValueError:
        try:
            canonical = date.fromisoformat(stripped).isoformat()
        except ValueError:
            return value, None
    if canonical == value:
        return value, None
    return canonical, RULE_NORMALIZE_DATE
