"""Whitespace detection (approved Module 14 requirement items 11-13).
Unlike the format/required/enum/outlier/primary-key rules, these three run
on every column unconditionally -- they are generic textual-formatting
checks that need no semantic type knowledge, so the approved correction
#5 column-rule gate does not apply to them. Each rule's suggested_fix is
the single, unambiguous mechanical repair (matching
app.cleaning.rules.trim_whitespace's own approach) -- advisory text only;
this module never writes it back anywhere (see this package's own
read-only guarantee)."""
from __future__ import annotations

import re
from typing import Iterator

from app.detection.issue_types import (
    LEADING_WHITESPACE,
    MULTIPLE_INTERNAL_SPACES,
    TRAILING_WHITESPACE,
)
from app.detection.severities import INFO
from app.detection.types import DetectionDataset, Finding

_INTERNAL_MULTISPACE_RE = re.compile(r"\S(\s{2,})\S")


def _csv_row_number(row_index: int) -> int:
    return row_index + 2


def _iter_nonblank_cells(dataset: DetectionDataset) -> Iterator[tuple[int, int, str]]:
    for row_index, row in enumerate(dataset.rows):
        for column_index, value in enumerate(row):
            if value != "":
                yield row_index, column_index, value


class LeadingWhitespaceRule:
    issue_type = LEADING_WHITESPACE

    def detect(self, dataset: DetectionDataset) -> Iterator[Finding]:
        for row_index, column_index, value in _iter_nonblank_cells(dataset):
            if value != value.lstrip():
                yield Finding(
                    row_number=_csv_row_number(row_index),
                    column_name=dataset.headers[column_index],
                    issue_type=self.issue_type,
                    severity=INFO,
                    original_value=value,
                    suggested_fix=value.lstrip(),
                    confidence=1.0,
                )


class TrailingWhitespaceRule:
    issue_type = TRAILING_WHITESPACE

    def detect(self, dataset: DetectionDataset) -> Iterator[Finding]:
        for row_index, column_index, value in _iter_nonblank_cells(dataset):
            if value != value.rstrip():
                yield Finding(
                    row_number=_csv_row_number(row_index),
                    column_name=dataset.headers[column_index],
                    issue_type=self.issue_type,
                    severity=INFO,
                    original_value=value,
                    suggested_fix=value.rstrip(),
                    confidence=1.0,
                )


class MultipleInternalSpacesRule:
    issue_type = MULTIPLE_INTERNAL_SPACES

    def detect(self, dataset: DetectionDataset) -> Iterator[Finding]:
        for row_index, column_index, value in _iter_nonblank_cells(dataset):
            if _INTERNAL_MULTISPACE_RE.search(value):
                collapsed = re.sub(r"(\S)\s{2,}(\S)", r"\1 \2", value)
                yield Finding(
                    row_number=_csv_row_number(row_index),
                    column_name=dataset.headers[column_index],
                    issue_type=self.issue_type,
                    severity=INFO,
                    original_value=value,
                    suggested_fix=collapsed,
                    confidence=1.0,
                )
