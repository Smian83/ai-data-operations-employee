"""Inconsistent-capitalization detection (approved Module 14 requirement
item 14). Disabled by default and only ever runs on a column whose
IssueDetectionColumnRule.capitalization_check_enabled is explicitly true
(approved correction #6 -- prevents false positives on names, IDs,
product codes, and acronyms, which is exactly why this is opt-in per
column rather than automatic).

Deterministic dominant-pattern algorithm: classify every non-blank,
alphabetic-containing value in an enabled column into one of four casing
patterns (lower / upper / title / mixed), then flag every value whose
pattern differs from whichever pattern occurs most often in that column.
Values with no alphabetic characters at all (pure numbers, punctuation)
are skipped entirely -- they have no casing to be inconsistent about."""
from __future__ import annotations

from collections import Counter
from typing import Iterator

from app.detection.issue_types import INCONSISTENT_CAPITALIZATION
from app.detection.severities import LOW
from app.detection.types import DetectionDataset, Finding

_LOWER = "lower"
_UPPER = "upper"
_TITLE = "title"
_MIXED = "mixed"
# Fixed priority order for deterministic tie-breaking when two or more
# patterns tie for the highest count in a column -- never dependent on
# dict/Counter iteration order, which Python does not guarantee is stable
# across equal-count ties the way this explicit tuple is.
_PATTERN_PRIORITY = (_LOWER, _TITLE, _UPPER, _MIXED)


def _csv_row_number(row_index: int) -> int:
    return row_index + 2


def _classify_casing(value: str) -> str | None:
    if not any(char.isalpha() for char in value):
        return None
    if value == value.lower():
        return _LOWER
    if value == value.upper():
        return _UPPER
    if value == value.title():
        return _TITLE
    return _MIXED


def _dominant_pattern(counts: Counter) -> str:
    max_count = max(counts.values())
    for pattern in _PATTERN_PRIORITY:
        if counts.get(pattern, 0) == max_count:
            return pattern
    raise AssertionError("unreachable -- _PATTERN_PRIORITY covers every classify_casing result")


class InconsistentCapitalizationRule:
    issue_type = INCONSISTENT_CAPITALIZATION

    def detect(self, dataset: DetectionDataset) -> Iterator[Finding]:
        enabled_columns = [
            column_index
            for column_index, header in enumerate(dataset.headers)
            if (rule := dataset.column_rules.get(header)) is not None
            and rule.capitalization_check_enabled
        ]
        for column_index in enabled_columns:
            yield from self._detect_column(dataset, column_index)

    def _detect_column(self, dataset: DetectionDataset, column_index: int) -> Iterator[Finding]:
        patterns: dict[int, str] = {}
        counts: Counter = Counter()
        for row_index, row in enumerate(dataset.rows):
            pattern = _classify_casing(row[column_index])
            if pattern is not None:
                patterns[row_index] = pattern
                counts[pattern] += 1
        if not counts:
            return
        dominant = _dominant_pattern(counts)
        for row_index, pattern in patterns.items():
            if pattern != dominant:
                yield Finding(
                    row_number=_csv_row_number(row_index),
                    column_name=dataset.headers[column_index],
                    issue_type=self.issue_type,
                    severity=LOW,
                    original_value=dataset.rows[row_index][column_index],
                    suggested_fix=None,
                    confidence=0.6,
                )
