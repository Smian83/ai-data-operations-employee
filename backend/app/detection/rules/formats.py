"""Format validity detection for email/phone/date/numeric columns
(approved Module 14 requirements items 6-9). Every rule here runs on a
column ONLY when that column's IssueDetectionColumnRule.expected_type
explicitly names it (approved correction #5) -- never inferred from the
column's name or its values. Empty cells are left to
app.detection.rules.blank_values entirely -- a format check has nothing
to validate against nothing."""
from __future__ import annotations

from typing import Iterator

from app.detection.issue_types import INVALID_DATE, INVALID_EMAIL, INVALID_NUMERIC, INVALID_PHONE
from app.detection.severities import MEDIUM
from app.detection.types import DetectionDataset, Finding
from app.detection.validators import (
    is_valid_email_syntax,
    is_valid_iso_date,
    is_valid_numeric,
    is_valid_phone_international,
)


def _csv_row_number(row_index: int) -> int:
    return row_index + 2


class _ExpectedTypeRule:
    """Shared iteration shape for every rule in this module -- each
    subclass only supplies which expected_type it governs and how to
    validate a non-empty value. Not part of the DetectionRule interface
    itself (that stays a plain Protocol, see app.detection.base); this is
    purely this module's own internal deduplication, invisible to the
    engine and to app.detection.registry."""

    issue_type: str
    expected_type: str
    severity = MEDIUM
    confidence = 1.0

    def _is_valid(self, value: str) -> bool:
        raise NotImplementedError

    def detect(self, dataset: DetectionDataset) -> Iterator[Finding]:
        target_columns = [
            column_index
            for column_index, header in enumerate(dataset.headers)
            if (rule := dataset.column_rules.get(header)) is not None
            and rule.expected_type == self.expected_type
        ]
        if not target_columns:
            return
        for row_index, row in enumerate(dataset.rows):
            for column_index in target_columns:
                value = row[column_index]
                if value == "" or self._is_valid(value):
                    continue
                yield Finding(
                    row_number=_csv_row_number(row_index),
                    column_name=dataset.headers[column_index],
                    issue_type=self.issue_type,
                    severity=self.severity,
                    original_value=value,
                    suggested_fix=None,
                    confidence=self.confidence,
                )


class InvalidEmailRule(_ExpectedTypeRule):
    issue_type = INVALID_EMAIL
    expected_type = "email"

    def _is_valid(self, value: str) -> bool:
        return is_valid_email_syntax(value)


class InvalidPhoneRule(_ExpectedTypeRule):
    issue_type = INVALID_PHONE
    expected_type = "phone"
    # Lower than the other three format rules: a phone number lacking an
    # explicit country code (see app.detection.validators.
    # is_valid_phone_international's own docstring) is flagged here even
    # though it might be a perfectly valid local number -- Module 14 has
    # no per-row country context to check it against, so this rule is
    # deliberately less certain than a pure syntax check.
    confidence = 0.7

    def _is_valid(self, value: str) -> bool:
        return is_valid_phone_international(value)


class InvalidDateRule(_ExpectedTypeRule):
    issue_type = INVALID_DATE
    expected_type = "date"

    def _is_valid(self, value: str) -> bool:
        return is_valid_iso_date(value)


class InvalidNumericRule(_ExpectedTypeRule):
    issue_type = INVALID_NUMERIC
    expected_type = "numeric"

    def _is_valid(self, value: str) -> bool:
        return is_valid_numeric(value)
