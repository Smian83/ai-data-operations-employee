"""Configurable outlier detection (approved Module 14 requirement item
17). Runs ONLY on a column whose IssueDetectionColumnRule.outlier_enabled
is explicitly true (approved correction #5) -- never automatic, even for
an obviously numeric column. Values that fail to parse as a number are
silently skipped for THIS rule (they are not double-flagged here; a
column explicitly declared expected_type="numeric" gets its own
independent invalid_numeric findings from app.detection.rules.formats).

Deterministic modified z-score (median / MAD), the Iglewicz & Hoaglin
method: for each value x, z = 0.6745 * (x - median) / MAD, where MAD is
the median absolute deviation from the column's own median. This method
is used (rather than a mean/standard-deviation z-score) because it is
itself robust to the outliers it is trying to find -- a mean/stdev score
can be dragged so far by a single extreme value that it fails to flag it.
A column with zero variance (MAD == 0) has no computable outliers by this
method and is skipped entirely, deterministically, rather than dividing
by zero.

Like every other rule, this one takes only a DetectionDataset -- no
separate limits/config argument (see app.detection.base.DetectionRule).
The per-column threshold is read from ColumnRuleConfig.
outlier_zscore_threshold, which app.worker.handlers.issue_detection
always resolves to a concrete value (falling back to
settings.issue_detection_outlier_zscore_threshold) before this rule ever
runs; _FALLBACK_ZSCORE_THRESHOLD below only guards a directly-constructed
ColumnRuleConfig (e.g. in a unit test) that left it unset."""
from __future__ import annotations

from typing import Iterator

from app.detection.issue_types import OUTLIER
from app.detection.severities import MEDIUM
from app.detection.types import DetectionDataset, Finding
from app.detection.validators import is_valid_numeric

_MODIFIED_ZSCORE_CONSTANT = 0.6745
# Matches Settings.issue_detection_outlier_zscore_threshold's own default
# (app.core.config) -- see this module's docstring for when this is
# actually used.
_FALLBACK_ZSCORE_THRESHOLD = 3.5


def _csv_row_number(row_index: int) -> int:
    return row_index + 2


def _median(values: list[float]) -> float:
    ordered = sorted(values)
    n = len(ordered)
    mid = n // 2
    if n % 2 == 1:
        return ordered[mid]
    return (ordered[mid - 1] + ordered[mid]) / 2


class OutlierRule:
    issue_type = OUTLIER

    def detect(self, dataset: DetectionDataset) -> Iterator[Finding]:
        target_columns = [
            column_index
            for column_index, header in enumerate(dataset.headers)
            if (rule := dataset.column_rules.get(header)) is not None and rule.outlier_enabled
        ]
        for column_index in target_columns:
            yield from self._detect_column(dataset, column_index)

    def _detect_column(self, dataset: DetectionDataset, column_index: int) -> Iterator[Finding]:
        header = dataset.headers[column_index]
        column_rule = dataset.column_rules[header]
        threshold = column_rule.outlier_zscore_threshold or _FALLBACK_ZSCORE_THRESHOLD

        numeric_by_row: dict[int, float] = {}
        for row_index, row in enumerate(dataset.rows):
            value = row[column_index]
            if value != "" and is_valid_numeric(value):
                numeric_by_row[row_index] = float(value.strip().replace(",", ""))
        if len(numeric_by_row) < 2:
            return

        values = list(numeric_by_row.values())
        median = _median(values)
        absolute_deviations = [abs(v - median) for v in values]
        mad = _median(absolute_deviations)
        if mad == 0:
            return

        for row_index, value in numeric_by_row.items():
            z = _MODIFIED_ZSCORE_CONSTANT * (value - median) / mad
            if abs(z) > threshold:
                # Deterministic, bounded confidence: scales with how far
                # past the threshold the value sits, capped at 1.0 -- never
                # a raw unbounded z-score.
                confidence = min(1.0, abs(z) / (threshold * 2))
                yield Finding(
                    row_number=_csv_row_number(row_index),
                    column_name=header,
                    issue_type=self.issue_type,
                    severity=MEDIUM,
                    original_value=dataset.rows[row_index][column_index],
                    suggested_fix=None,
                    confidence=round(confidence, 4),
                )
