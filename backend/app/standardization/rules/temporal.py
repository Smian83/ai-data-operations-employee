"""
Date / time standardization (design doc Section 6). Canonical internal
representation is ISO-8601 for both, reparsed via the same
datetime.fromisoformat-based approach app.cleaning.rules._coerce_date
established -- implemented here as Module 7's own pure function rather
than importing Module 6's, since the two are scoped to different inputs
(Module 6: already-profiled inferred_type columns; Module 7: field_type-
classified columns) and conflating them via a cross-module import would
couple two independently-versioned rule sets together. Unparseable
values are left untouched, never guessed.
"""
from __future__ import annotations

from datetime import date, datetime, time

from app.standardization.rules.constants import RULE_DATE_FORMAT_NORMALIZE, RULE_TIME_FORMAT_NORMALIZE


def standardize_date(value: str, output_format: str | None) -> tuple[str, str | None]:
    """output_format is a strftime format string (StandardizationConfig.
    date_output_format); None means the canonical ISO-8601 form itself is
    the desired output."""
    if value == "":
        return value, None
    stripped = value.strip()
    parsed: datetime | date | None = None
    try:
        parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = date.fromisoformat(stripped)
        except ValueError:
            return value, None

    if output_format:
        canonical = parsed.strftime(output_format)
    else:
        canonical = parsed.isoformat()

    if canonical == value:
        return value, None
    return canonical, RULE_DATE_FORMAT_NORMALIZE


def standardize_time(value: str) -> tuple[str, str | None]:
    if value == "":
        return value, None
    stripped = value.strip()
    parsed: time | None = None
    for candidate in (stripped, stripped.upper().replace(" ", "")):
        try:
            parsed = time.fromisoformat(candidate)
            break
        except ValueError:
            continue
    if parsed is None:
        return value, None

    canonical = parsed.isoformat()
    if canonical == value:
        return value, None
    return canonical, RULE_TIME_FORMAT_NORMALIZE
