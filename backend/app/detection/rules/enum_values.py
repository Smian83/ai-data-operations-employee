"""Invalid enum/category value detection (approved Module 14 requirement
item 16). Runs ONLY on a column whose IssueDetectionColumnRule.
allowed_values is explicitly set (approved correction #5) -- an
unconfigured column has nothing to be "invalid" against. Matching is
exact and case-sensitive, since allowed_values is an operator-supplied
literal list, not a heuristic; empty cells are left entirely to
app.detection.rules.blank_values/required, not flagged here."""
from __future__ import annotations

from typing import Iterator

from app.detection.issue_types import INVALID_ENUM_VALUE
from app.detection.severities import HIGH
from app.detection.types import DetectionDataset, Finding


def _csv_row_number(row_index: int) -> int:
    return row_index + 2


class InvalidEnumValueRule:
    issue_type = INVALID_ENUM_VALUE

    def detect(self, dataset: DetectionDataset) -> Iterator[Finding]:
        target_columns = [
            (column_index, set(rule.allowed_values))
            for column_index, header in enumerate(dataset.headers)
            if (rule := dataset.column_rules.get(header)) is not None and rule.allowed_values
        ]
        if not target_columns:
            return
        for row_index, row in enumerate(dataset.rows):
            for column_index, allowed in target_columns:
                value = row[column_index]
                if value != "" and value not in allowed:
                    yield Finding(
                        row_number=_csv_row_number(row_index),
                        column_name=dataset.headers[column_index],
                        issue_type=self.issue_type,
                        severity=HIGH,
                        original_value=value,
                        suggested_fix=None,
                        confidence=1.0,
                    )
