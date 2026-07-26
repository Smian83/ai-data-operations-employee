"""Missing / empty / null detection -- three independent, mutually
exclusive classifications of "this cell has no real value" (approved
Module 14 requirements items 1-3). A cell can only ever match one of the
three: missing_value (structurally absent -- the source row had too few
fields and app.profiling.csv_loader.load_csv padded it in), else
empty_string (the source row genuinely had "" here), else null_value (a
recognized null-sentinel token). Every rule in this module reports CSV
line numbers (row_number = its 0-based index into DetectionDataset.rows,
plus 2 -- row 1 is the header, so the first data row is row 2), the same
convention app.profiling.csv_loader.load_csv's own structural_issues
already use, so missing_value's row_number values line up with them
directly."""
from __future__ import annotations

from typing import Iterator

from app.detection.issue_types import EMPTY_STRING, MISSING_VALUE, NULL_VALUE
from app.detection.severities import LOW, MEDIUM
from app.detection.types import DetectionDataset, Finding

# Same idea as app.cleaning.rules._BLANK_EQUIVALENTS, kept as Module 14's
# own copy per this package's established "each module owns its pure
# constants" precedent (see app.detection.validators' own docstring) --
# 'nan'/'nil' added since Module 14 scans raw, unstandardized source data,
# where those appear more often than in Module 6's already-profiled input.
NULL_SENTINELS = frozenset({"null", "none", "n/a", "na", "-", "nan", "nil"})


def _csv_row_number(row_index: int) -> int:
    return row_index + 2


class MissingValueRule:
    issue_type = MISSING_VALUE

    def detect(self, dataset: DetectionDataset) -> Iterator[Finding]:
        # Map each too_few_fields structural issue to the exact (row,
        # column) cells load_csv padded in -- field_count is how many
        # real fields the source row actually had, so every column index
        # from there to the end of the header row was synthesized, never
        # present in the source file.
        for issue in dataset.structural_issues:
            if issue.get("type") != "too_few_fields":
                continue
            row_number = issue["row_number"]
            field_count = issue["field_count"]
            for column_index in range(field_count, len(dataset.headers)):
                yield Finding(
                    row_number=row_number,
                    column_name=dataset.headers[column_index],
                    issue_type=self.issue_type,
                    severity=MEDIUM,
                    original_value=None,
                    suggested_fix=None,
                    confidence=1.0,
                )


class EmptyStringRule:
    issue_type = EMPTY_STRING

    def detect(self, dataset: DetectionDataset) -> Iterator[Finding]:
        missing_cells = _missing_cells(dataset)
        for row_index, row in enumerate(dataset.rows):
            for column_index, value in enumerate(row):
                if (row_index, column_index) in missing_cells:
                    continue  # already classified as missing_value above
                if value == "":
                    yield Finding(
                        row_number=_csv_row_number(row_index),
                        column_name=dataset.headers[column_index],
                        issue_type=self.issue_type,
                        severity=LOW,
                        original_value="",
                        suggested_fix=None,
                        confidence=1.0,
                    )


class NullValueRule:
    issue_type = NULL_VALUE

    def detect(self, dataset: DetectionDataset) -> Iterator[Finding]:
        for row_index, row in enumerate(dataset.rows):
            for column_index, value in enumerate(row):
                if value != "" and value.strip().casefold() in NULL_SENTINELS:
                    yield Finding(
                        row_number=_csv_row_number(row_index),
                        column_name=dataset.headers[column_index],
                        issue_type=self.issue_type,
                        severity=LOW,
                        original_value=value,
                        suggested_fix=None,
                        confidence=1.0,
                    )


def _missing_cells(dataset: DetectionDataset) -> set[tuple[int, int]]:
    """The exact (row_index, column_index) pairs MissingValueRule would
    flag -- reused by EmptyStringRule so a padded cell is never
    double-classified as an empty string too. row_index here is the
    0-based DetectionDataset.rows index (not the CSV row_number), since
    that is what EmptyStringRule's own enumeration uses."""
    cells: set[tuple[int, int]] = set()
    for issue in dataset.structural_issues:
        if issue.get("type") != "too_few_fields":
            continue
        row_index = issue["row_number"] - 2
        field_count = issue["field_count"]
        for column_index in range(field_count, len(dataset.headers)):
            cells.add((row_index, column_index))
    return cells
