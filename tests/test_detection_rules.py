"""Module 14 Phase 2 unit tests: each detection rule in isolation, one
test group per rule. Pure -- no DB, no client, no fixtures beyond a
directly-constructed DetectionDataset/ColumnRuleConfig. Every rule is
exercised for: the config-gate (does nothing when unconfigured, where
applicable), the positive case (flags what it should), and the
already-clean/no-op case (flags nothing on conforming data)."""
from app.detection.issue_types import (
    BOOLEAN_INCONSISTENCY,
    BROKEN_FK_REFERENCE,
    DUPLICATE_PRIMARY_KEY,
    DUPLICATE_ROW,
    EMPTY_STRING,
    INCONSISTENT_CAPITALIZATION,
    INVALID_DATE,
    INVALID_EMAIL,
    INVALID_ENUM_VALUE,
    INVALID_NUMERIC,
    INVALID_PHONE,
    LEADING_WHITESPACE,
    MISSING_VALUE,
    MULTIPLE_INTERNAL_SPACES,
    NULL_VALUE,
    OUTLIER,
    REQUIRED_FIELD_VIOLATION,
    TRAILING_WHITESPACE,
)
from app.detection.rules.blank_values import EmptyStringRule, MissingValueRule, NullValueRule
from app.detection.rules.boolean import BooleanInconsistencyRule
from app.detection.rules.capitalization import InconsistentCapitalizationRule
from app.detection.rules.duplicates import DuplicatePrimaryKeyRule, DuplicateRowRule
from app.detection.rules.enum_values import InvalidEnumValueRule
from app.detection.rules.foreign_keys import BrokenFkReferenceRule
from app.detection.rules.formats import (
    InvalidDateRule,
    InvalidEmailRule,
    InvalidNumericRule,
    InvalidPhoneRule,
)
from app.detection.rules.outliers import OutlierRule
from app.detection.rules.required import RequiredFieldViolationRule
from app.detection.rules.whitespace import (
    LeadingWhitespaceRule,
    MultipleInternalSpacesRule,
    TrailingWhitespaceRule,
)
from app.detection.types import ColumnRuleConfig, DetectionDataset


def _dataset(headers, rows, structural_issues=None, column_rules=None) -> DetectionDataset:
    return DetectionDataset(
        headers=headers,
        rows=rows,
        structural_issues=structural_issues or [],
        column_rules=column_rules or {},
    )


# --- MissingValueRule ---------------------------------------------------------


def test_missing_value_flags_only_structurally_padded_cells():
    dataset = _dataset(
        headers=["id", "name", "email"],
        rows=[["1", "Ada", ""]],
        structural_issues=[{"type": "too_few_fields", "row_number": 2, "field_count": 2}],
    )
    findings = list(MissingValueRule().detect(dataset))
    assert len(findings) == 1
    assert findings[0].row_number == 2
    assert findings[0].column_name == "email"
    assert findings[0].issue_type == MISSING_VALUE
    assert findings[0].original_value is None


def test_missing_value_ignores_too_many_fields_and_normal_rows():
    dataset = _dataset(
        headers=["id", "name"],
        rows=[["1", "Ada"]],
        structural_issues=[{"type": "too_many_fields", "row_number": 2, "field_count": 3}],
    )
    assert list(MissingValueRule().detect(dataset)) == []


# --- EmptyStringRule -----------------------------------------------------------


def test_empty_string_flags_genuinely_blank_cell():
    dataset = _dataset(headers=["id", "name"], rows=[["1", ""]])
    findings = list(EmptyStringRule().detect(dataset))
    assert len(findings) == 1
    assert findings[0].column_name == "name"
    assert findings[0].issue_type == EMPTY_STRING


def test_empty_string_does_not_double_flag_a_structurally_padded_cell():
    dataset = _dataset(
        headers=["id", "name"],
        rows=[["1", ""]],
        structural_issues=[{"type": "too_few_fields", "row_number": 2, "field_count": 1}],
    )
    # column index 1 ("name") is the padded cell -- MissingValueRule owns
    # it, EmptyStringRule must skip it entirely.
    assert list(EmptyStringRule().detect(dataset)) == []


# --- NullValueRule ---------------------------------------------------------------


def test_null_value_recognizes_known_sentinels_case_insensitively():
    dataset = _dataset(headers=["a"], rows=[["N/A"], ["null"], ["NONE"], ["real value"]])
    findings = list(NullValueRule().detect(dataset))
    assert len(findings) == 3
    assert all(f.issue_type == NULL_VALUE for f in findings)


# --- DuplicateRowRule --------------------------------------------------------------


def test_duplicate_row_flags_second_and_later_occurrences_only():
    dataset = _dataset(headers=["a", "b"], rows=[["1", "x"], ["1", "x"], ["2", "y"], ["1", "x"]])
    findings = list(DuplicateRowRule().detect(dataset))
    assert [f.row_number for f in findings] == [3, 5]
    assert all(f.issue_type == DUPLICATE_ROW and f.column_name is None for f in findings)


def test_duplicate_row_is_noop_on_all_unique_rows():
    dataset = _dataset(headers=["a"], rows=[["1"], ["2"], ["3"]])
    assert list(DuplicateRowRule().detect(dataset)) == []


# --- DuplicatePrimaryKeyRule --------------------------------------------------------


def test_duplicate_primary_key_requires_explicit_configuration():
    dataset = _dataset(headers=["id", "name"], rows=[["1", "a"], ["1", "b"]])
    # No column_rules at all -- must never guess "id" is a primary key.
    assert list(DuplicatePrimaryKeyRule().detect(dataset)) == []


def test_duplicate_primary_key_flags_repeated_composite_key():
    dataset = _dataset(
        headers=["org", "id", "name"],
        rows=[["a", "1", "x"], ["a", "1", "y"], ["b", "1", "z"]],
        column_rules={
            "org": ColumnRuleConfig(is_primary_key=True),
            "id": ColumnRuleConfig(is_primary_key=True),
        },
    )
    findings = list(DuplicatePrimaryKeyRule().detect(dataset))
    assert len(findings) == 1
    assert findings[0].row_number == 3
    assert findings[0].column_name == "org+id"
    assert findings[0].issue_type == DUPLICATE_PRIMARY_KEY
    assert findings[0].severity == "CRITICAL"


# --- Format rules (email/phone/date/numeric) ----------------------------------------


def test_invalid_email_runs_only_on_configured_column():
    dataset = _dataset(headers=["email"], rows=[["not-an-email"]])
    assert list(InvalidEmailRule().detect(dataset)) == []  # unconfigured -- never guessed


def test_invalid_email_flags_malformed_value_when_configured():
    dataset = _dataset(
        headers=["email"],
        rows=[["not-an-email"], ["good@example.com"], [""]],
        column_rules={"email": ColumnRuleConfig(expected_type="email")},
    )
    findings = list(InvalidEmailRule().detect(dataset))
    assert len(findings) == 1
    assert findings[0].row_number == 2
    assert findings[0].issue_type == INVALID_EMAIL


def test_invalid_phone_requires_leading_plus_and_uses_lower_confidence():
    dataset = _dataset(
        headers=["phone"],
        rows=[["5551234567"], ["+14155552671"]],
        column_rules={"phone": ColumnRuleConfig(expected_type="phone")},
    )
    findings = list(InvalidPhoneRule().detect(dataset))
    assert len(findings) == 1
    assert findings[0].row_number == 2
    assert findings[0].issue_type == INVALID_PHONE
    assert findings[0].confidence == 0.7


def test_invalid_date_accepts_iso_and_flags_non_iso():
    dataset = _dataset(
        headers=["d"],
        rows=[["2024-01-15"], ["01/15/2024"]],
        column_rules={"d": ColumnRuleConfig(expected_type="date")},
    )
    findings = list(InvalidDateRule().detect(dataset))
    assert len(findings) == 1
    assert findings[0].row_number == 3
    assert findings[0].issue_type == INVALID_DATE


def test_invalid_numeric_accepts_thousands_grouping_and_flags_words():
    dataset = _dataset(
        headers=["amount"],
        rows=[["1,234,567"], ["forty"]],
        column_rules={"amount": ColumnRuleConfig(expected_type="numeric")},
    )
    findings = list(InvalidNumericRule().detect(dataset))
    assert len(findings) == 1
    assert findings[0].row_number == 3
    assert findings[0].issue_type == INVALID_NUMERIC


# --- RequiredFieldViolationRule -------------------------------------------------------


def test_required_field_violation_gated_by_is_required():
    dataset = _dataset(headers=["name"], rows=[[""]])
    assert list(RequiredFieldViolationRule().detect(dataset)) == []


def test_required_field_violation_flags_blank_and_null_sentinel():
    dataset = _dataset(
        headers=["name"],
        rows=[[""], ["N/A"], ["Ada"]],
        column_rules={"name": ColumnRuleConfig(is_required=True)},
    )
    findings = list(RequiredFieldViolationRule().detect(dataset))
    assert [f.row_number for f in findings] == [2, 3]
    assert all(f.issue_type == REQUIRED_FIELD_VIOLATION and f.severity == "HIGH" for f in findings)


# --- Whitespace rules (unconditional) --------------------------------------------------


def test_leading_and_trailing_whitespace_flagged_unconditionally():
    dataset = _dataset(headers=["name"], rows=[[" Ada"], ["Grace "], ["Bob"]])
    leading = list(LeadingWhitespaceRule().detect(dataset))
    trailing = list(TrailingWhitespaceRule().detect(dataset))
    assert [f.row_number for f in leading] == [2]
    assert leading[0].suggested_fix == "Ada"
    assert leading[0].issue_type == LEADING_WHITESPACE
    assert [f.row_number for f in trailing] == [3]
    assert trailing[0].suggested_fix == "Grace"
    assert trailing[0].issue_type == TRAILING_WHITESPACE


def test_multiple_internal_spaces_flags_and_collapses():
    dataset = _dataset(headers=["name"], rows=[["Bob  Jones"], ["Ada Lovelace"]])
    findings = list(MultipleInternalSpacesRule().detect(dataset))
    assert len(findings) == 1
    assert findings[0].suggested_fix == "Bob Jones"
    assert findings[0].issue_type == MULTIPLE_INTERNAL_SPACES


# --- InconsistentCapitalizationRule ------------------------------------------------------


def test_capitalization_disabled_by_default():
    dataset = _dataset(headers=["name"], rows=[["ada"], ["GRACE"], ["Bob"]])
    assert list(InconsistentCapitalizationRule().detect(dataset)) == []


def test_capitalization_flags_minority_pattern_when_enabled():
    dataset = _dataset(
        headers=["name"],
        rows=[["Ada Lovelace"], ["Grace Hopper"], ["bob jones"]],
        column_rules={"name": ColumnRuleConfig(capitalization_check_enabled=True)},
    )
    findings = list(InconsistentCapitalizationRule().detect(dataset))
    assert len(findings) == 1
    assert findings[0].row_number == 4
    assert findings[0].issue_type == INCONSISTENT_CAPITALIZATION
    assert findings[0].confidence == 0.6


# --- BooleanInconsistencyRule -----------------------------------------------------------


def test_boolean_inconsistency_gated_by_expected_type():
    dataset = _dataset(headers=["active"], rows=[["maybe"]])
    assert list(BooleanInconsistencyRule().detect(dataset)) == []


def test_boolean_inconsistency_flags_unrecognized_and_minority_family():
    dataset = _dataset(
        headers=["active"],
        rows=[["true"], ["false"], ["true"], ["1"], ["maybe"]],
        column_rules={"active": ColumnRuleConfig(expected_type="boolean")},
    )
    findings = list(BooleanInconsistencyRule().detect(dataset))
    by_row = {f.row_number: f for f in findings}
    assert set(by_row) == {6, 5}  # row 6 = "maybe" (unrecognized), row 5 = "1" (minority family)
    assert by_row[6].severity == "HIGH" and by_row[6].confidence == 1.0
    assert by_row[5].severity == "MEDIUM" and by_row[5].confidence == 0.7
    assert all(f.issue_type == BOOLEAN_INCONSISTENCY for f in findings)


# --- InvalidEnumValueRule --------------------------------------------------------------


def test_invalid_enum_value_gated_by_allowed_values():
    dataset = _dataset(headers=["status"], rows=[["bogus"]])
    assert list(InvalidEnumValueRule().detect(dataset)) == []


def test_invalid_enum_value_flags_value_outside_allowed_set():
    dataset = _dataset(
        headers=["status"],
        rows=[["active"], ["bogus"], [""]],
        column_rules={"status": ColumnRuleConfig(allowed_values=("active", "inactive"))},
    )
    findings = list(InvalidEnumValueRule().detect(dataset))
    assert len(findings) == 1
    assert findings[0].row_number == 3
    assert findings[0].issue_type == INVALID_ENUM_VALUE
    assert findings[0].severity == "HIGH"


# --- OutlierRule ------------------------------------------------------------------------


def test_outlier_gated_by_outlier_enabled():
    dataset = _dataset(headers=["amount"], rows=[["10"], ["11"], ["10000"]])
    assert list(OutlierRule().detect(dataset)) == []


def test_outlier_flags_extreme_value_via_modified_zscore():
    dataset = _dataset(
        headers=["amount"],
        rows=[["10"], ["11"], ["9"], ["10"], ["10000"]],
        column_rules={
            "amount": ColumnRuleConfig(outlier_enabled=True, outlier_zscore_threshold=3.5)
        },
    )
    findings = list(OutlierRule().detect(dataset))
    assert len(findings) == 1
    assert findings[0].row_number == 6
    assert findings[0].issue_type == OUTLIER
    assert 0.0 < findings[0].confidence <= 1.0


def test_outlier_skips_non_numeric_and_zero_variance_columns():
    dataset = _dataset(
        headers=["amount"],
        rows=[["10"], ["10"], ["10"]],
        column_rules={"amount": ColumnRuleConfig(outlier_enabled=True)},
    )
    # MAD == 0 -- must not divide by zero, must yield nothing.
    assert list(OutlierRule().detect(dataset)) == []


# --- BrokenFkReferenceRule (deferred) ----------------------------------------------------


def test_broken_fk_reference_is_registered_but_yields_nothing():
    rule = BrokenFkReferenceRule()
    assert rule.issue_type == BROKEN_FK_REFERENCE
    dataset = _dataset(headers=["a"], rows=[["1"]])
    assert list(rule.detect(dataset)) == []
