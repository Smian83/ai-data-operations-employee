"""Whole-row and primary-key duplicate detection (approved Module 14
requirements items 4-5). Both compare already-loaded rows as exact tuples
-- same normalized-tuple-comparison approach
app.profiling.csv_profiler.profile_csv and app.cleaning.engine._count_
duplicates each already use, applied here to flag every individual
duplicate row/key instead of only counting them."""
from __future__ import annotations

from typing import Iterator

from app.detection.issue_types import DUPLICATE_PRIMARY_KEY, DUPLICATE_ROW
from app.detection.severities import CRITICAL, MEDIUM
from app.detection.types import DetectionDataset, Finding


def _csv_row_number(row_index: int) -> int:
    return row_index + 2


class DuplicateRowRule:
    issue_type = DUPLICATE_ROW

    def detect(self, dataset: DetectionDataset) -> Iterator[Finding]:
        seen: set[tuple[str, ...]] = set()
        for row_index, row in enumerate(dataset.rows):
            key = tuple(row)
            if key in seen:
                yield Finding(
                    row_number=_csv_row_number(row_index),
                    column_name=None,
                    issue_type=self.issue_type,
                    severity=MEDIUM,
                    original_value=None,
                    suggested_fix=None,
                    confidence=1.0,
                )
            else:
                seen.add(key)


class DuplicatePrimaryKeyRule:
    issue_type = DUPLICATE_PRIMARY_KEY

    def detect(self, dataset: DetectionDataset) -> Iterator[Finding]:
        # Per the approved Module 14 corrections: a column only
        # participates in this check when its own IssueDetectionColumnRule
        # explicitly marks it is_primary_key -- never inferred from a
        # column named "id" or similar. A composite key uses every such
        # column, in header order (deterministic and independent of the
        # order rows happen to define them in the config table).
        key_columns = [
            column_index
            for column_index, header in enumerate(dataset.headers)
            if (rule := dataset.column_rules.get(header)) is not None and rule.is_primary_key
        ]
        if not key_columns:
            return  # nothing configured -- never a guess.

        key_column_names = "+".join(dataset.headers[i] for i in key_columns)
        seen: set[tuple[str, ...]] = set()
        for row_index, row in enumerate(dataset.rows):
            key = tuple(row[i] for i in key_columns)
            if key in seen:
                yield Finding(
                    row_number=_csv_row_number(row_index),
                    column_name=key_column_names,
                    issue_type=self.issue_type,
                    severity=CRITICAL,
                    original_value=", ".join(key),
                    suggested_fix=None,
                    confidence=1.0,
                )
            else:
                seen.add(key)
