"""Required-field-violation detection (approved Module 14 requirement item
10). Runs ONLY on a column whose IssueDetectionColumnRule.is_required is
true -- never inferred from a column being non-empty elsewhere in the
data. A required column's value is a violation whenever it is missing
(structurally absent), an empty string, or a recognized null-sentinel --
the same three states app.detection.rules.blank_values independently
detects on every column; this rule adds one more, higher-severity finding
on top for the subset of columns explicitly marked required."""
from __future__ import annotations

from typing import Iterator

from app.detection.issue_types import REQUIRED_FIELD_VIOLATION
from app.detection.rules.blank_values import NULL_SENTINELS
from app.detection.severities import HIGH
from app.detection.types import DetectionDataset, Finding


def _csv_row_number(row_index: int) -> int:
    return row_index + 2


def _is_blank(value: str) -> bool:
    return value == "" or value.strip().casefold() in NULL_SENTINELS


class RequiredFieldViolationRule:
    issue_type = REQUIRED_FIELD_VIOLATION

    def detect(self, dataset: DetectionDataset) -> Iterator[Finding]:
        required_columns = [
            column_index
            for column_index, header in enumerate(dataset.headers)
            if (rule := dataset.column_rules.get(header)) is not None and rule.is_required
        ]
        if not required_columns:
            return
        for row_index, row in enumerate(dataset.rows):
            for column_index in required_columns:
                value = row[column_index]
                if _is_blank(value):
                    yield Finding(
                        row_number=_csv_row_number(row_index),
                        column_name=dataset.headers[column_index],
                        issue_type=self.issue_type,
                        severity=HIGH,
                        original_value=value if value != "" else None,
                        suggested_fix=None,
                        confidence=1.0,
                    )
