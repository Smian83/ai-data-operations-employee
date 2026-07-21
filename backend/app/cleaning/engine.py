"""
Module 6: orchestrates the fixed, ordered cleaning pipeline described in
docs/module-6-data-cleaning-engine-design.md Sections 8-9. `clean()` is
pure -- no I/O, no randomness -- so it always produces an identical
CleaningResult for identical input, which is this engine's determinism
acceptance criterion. CleaningHandler (app.worker.handlers.cleaning) is the
only caller; all file I/O and persistence happen there, not here.
"""
from __future__ import annotations

from app.cleaning import rules
from app.cleaning.types import Change, CleaningLimits, CleaningResult

# Bumped whenever the rule set in app.cleaning.rules changes in any way
# that could change output for existing input. Recorded on every
# CleaningRun (CleaningRun.cleaning_engine_version) so historical runs
# stay attributed to the exact engine version that actually produced them,
# even after this value changes -- see
# docs/module-6-data-cleaning-engine-design.md Section 15.
CLEANING_ENGINE_VERSION = "1.0"


def clean(
    rows: list[list[str]],
    headers: list[str],
    column_types: list[str],
    limits: CleaningLimits,
) -> CleaningResult:
    """`column_types[i]` is the DataProfile-reported inferred_type for
    `headers[i]` (the type-coercion context Section 9 requires). `rows`
    must already be uniform-length -- app.profiling.csv_loader.load_csv
    guarantees this before CleaningHandler ever calls here (see
    app.cleaning.rules' module docstring) -- so no structural-repair stage
    runs in this function."""
    cleaned_rows: list[list[str]] = []
    changes: list[Change] = []
    changes_by_rule: dict[str, int] = {}

    for row_index, row in enumerate(rows):
        cleaned_row = list(row)
        for column_index, raw_value in enumerate(row):
            column_name = headers[column_index]
            value = raw_value

            value, rule_name = rules.trim_whitespace(value)
            _record(changes, changes_by_rule, row_index, column_name, raw_value, value, rule_name)

            before = value
            value, rule_name = rules.normalize_blank(value)
            _record(changes, changes_by_rule, row_index, column_name, before, value, rule_name)

            before = value
            value, rule_name = rules.coerce_type(value, column_types[column_index])
            _record(changes, changes_by_rule, row_index, column_name, before, value, rule_name)

            cleaned_row[column_index] = value
        cleaned_rows.append(cleaned_row)

    duplicate_row_count = _count_duplicates(cleaned_rows)
    total_changes_count = len(changes)
    persisted_changes = changes[: limits.max_persisted_changes]
    confidence_score = min((change.confidence for change in changes), default=1.0)

    return CleaningResult(
        cleaned_rows=cleaned_rows,
        changes=persisted_changes,
        total_changes_count=total_changes_count,
        changes_by_rule=changes_by_rule,
        duplicate_row_count=duplicate_row_count,
        confidence_score=confidence_score,
    )


def _record(
    changes: list[Change],
    changes_by_rule: dict[str, int],
    row_index: int,
    column_name: str,
    before: str,
    after: str,
    rule_name: str | None,
) -> None:
    if rule_name is None or before == after:
        return
    changes.append(
        Change(
            row_index=row_index,
            column_name=column_name,
            original_value=before,
            cleaned_value=after,
            rule_name=rule_name,
            reason=rules.RULE_REASONS[rule_name],
            confidence=rules.RULE_CONFIDENCE[rule_name],
        )
    )
    changes_by_rule[rule_name] = changes_by_rule.get(rule_name, 0) + 1


def _count_duplicates(rows: list[list[str]]) -> int:
    """Rows that are exact duplicates of an earlier row, compared on
    already-cleaned values -- same normalized-tuple-comparison approach
    app.profiling.csv_profiler.profile_csv already uses for
    duplicate_row_count, applied here to the cleaned output instead of the
    raw input. Detection only: this module never removes rows
    automatically (Section 9)."""
    seen: set[tuple[str, ...]] = set()
    duplicate_count = 0
    for row in rows:
        key = tuple(row)
        if key in seen:
            duplicate_count += 1
        else:
            seen.add(key)
    return duplicate_count
