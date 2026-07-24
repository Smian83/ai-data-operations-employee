"""Boolean-inconsistency detection (approved Module 14 requirement item
15). Runs ONLY on a column whose IssueDetectionColumnRule.expected_type is
explicitly "boolean" (approved correction #5). Two independent findings,
both this same issue_type:
  - a value that matches no recognized boolean token at all (see
    app.detection.validators.classify_boolean_token) -- high confidence,
    this is unambiguously not a boolean representation;
  - a value that IS a recognized boolean token, but from a different
    token family than whichever family is dominant in this column (e.g.
    the column is mostly "true"/"false" but this row says "1") -- lower
    confidence, since mixing representations is a style inconsistency,
    not necessarily wrong data."""
from __future__ import annotations

from collections import Counter
from typing import Iterator

from app.detection.issue_types import BOOLEAN_INCONSISTENCY
from app.detection.severities import HIGH, MEDIUM
from app.detection.types import DetectionDataset, Finding
from app.detection.validators import classify_boolean_token


def _csv_row_number(row_index: int) -> int:
    return row_index + 2


class BooleanInconsistencyRule:
    issue_type = BOOLEAN_INCONSISTENCY

    def detect(self, dataset: DetectionDataset) -> Iterator[Finding]:
        target_columns = [
            column_index
            for column_index, header in enumerate(dataset.headers)
            if (rule := dataset.column_rules.get(header)) is not None
            and rule.expected_type == "boolean"
        ]
        for column_index in target_columns:
            yield from self._detect_column(dataset, column_index)

    def _detect_column(self, dataset: DetectionDataset, column_index: int) -> Iterator[Finding]:
        families: dict[int, str] = {}
        unrecognized: list[int] = []
        family_counts: Counter = Counter()
        for row_index, row in enumerate(dataset.rows):
            value = row[column_index]
            if value == "":
                continue
            family, _ = classify_boolean_token(value)
            if family is None:
                unrecognized.append(row_index)
            else:
                families[row_index] = family
                family_counts[family] += 1

        for row_index in unrecognized:
            yield Finding(
                row_number=_csv_row_number(row_index),
                column_name=dataset.headers[column_index],
                issue_type=self.issue_type,
                severity=HIGH,
                original_value=dataset.rows[row_index][column_index],
                suggested_fix=None,
                confidence=1.0,
            )

        if not family_counts:
            return
        # Deterministic tie-break: highest count wins; among ties, the
        # alphabetically-first family name wins -- never dependent on
        # Counter/dict iteration order.
        max_count = max(family_counts.values())
        dominant_family = min(f for f, c in family_counts.items() if c == max_count)

        for row_index, family in families.items():
            if family != dominant_family:
                yield Finding(
                    row_number=_csv_row_number(row_index),
                    column_name=dataset.headers[column_index],
                    issue_type=self.issue_type,
                    severity=MEDIUM,
                    original_value=dataset.rows[row_index][column_index],
                    suggested_fix=None,
                    confidence=0.7,
                )
