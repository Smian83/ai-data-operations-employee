"""
Named constants for every value in app.models.enums.ISSUE_TYPES -- the
single source of truth is still that tuple (it also drives the
ck_issues_issue_type_valid database CHECK constraint, see
app.models.issue); this module exists only so a detection rule can write
`issue_type = MISSING_VALUE` instead of a bare string literal (approved
Module 14 Phase 2 correction #2: "keep issue types as constants only, do
not scatter literal strings throughout the detection rules"). The
assertion below fails fast at import time if the two ever drift apart,
rather than silently producing a value the database would reject.
"""
from app.models.enums import ISSUE_TYPES

MISSING_VALUE = "missing_value"
EMPTY_STRING = "empty_string"
NULL_VALUE = "null_value"
DUPLICATE_ROW = "duplicate_row"
DUPLICATE_PRIMARY_KEY = "duplicate_primary_key"
INVALID_EMAIL = "invalid_email"
INVALID_PHONE = "invalid_phone"
INVALID_DATE = "invalid_date"
INVALID_NUMERIC = "invalid_numeric"
REQUIRED_FIELD_VIOLATION = "required_field_violation"
LEADING_WHITESPACE = "leading_whitespace"
TRAILING_WHITESPACE = "trailing_whitespace"
MULTIPLE_INTERNAL_SPACES = "multiple_internal_spaces"
INCONSISTENT_CAPITALIZATION = "inconsistent_capitalization"
BOOLEAN_INCONSISTENCY = "boolean_inconsistency"
INVALID_ENUM_VALUE = "invalid_enum_value"
OUTLIER = "outlier"
# Deferred -- see app.detection.rules.foreign_keys's own docstring and
# app.models.enums.ISSUE_TYPES's comment. Never produced by any rule in
# Module 14; the constant exists so a future module can reference it
# without inventing a new literal.
BROKEN_FK_REFERENCE = "broken_fk_reference"

_ALL = (
    MISSING_VALUE, EMPTY_STRING, NULL_VALUE, DUPLICATE_ROW, DUPLICATE_PRIMARY_KEY,
    INVALID_EMAIL, INVALID_PHONE, INVALID_DATE, INVALID_NUMERIC, REQUIRED_FIELD_VIOLATION,
    LEADING_WHITESPACE, TRAILING_WHITESPACE, MULTIPLE_INTERNAL_SPACES,
    INCONSISTENT_CAPITALIZATION, BOOLEAN_INCONSISTENCY, INVALID_ENUM_VALUE, OUTLIER,
    BROKEN_FK_REFERENCE,
)
assert set(_ALL) == set(ISSUE_TYPES), (
    "app.detection.issue_types has drifted from app.models.enums.ISSUE_TYPES"
)
assert len(_ALL) == len(ISSUE_TYPES) == 18
