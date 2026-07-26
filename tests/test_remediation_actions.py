"""Module 15 Phase 2 unit tests: one section per action, covering the
happy path, the no-op/ambiguous-input path (never guess), and the
missing-configuration skip path (never guess a default) where the action
has a gate at all. Exercises the propose_ functions directly (no engine
involved) so each action's own logic is isolated."""
import uuid

from app.detection.issue_types import (
    BOOLEAN_INCONSISTENCY,
    DUPLICATE_PRIMARY_KEY,
    DUPLICATE_ROW,
    INCONSISTENT_CAPITALIZATION,
    INVALID_DATE,
    INVALID_ENUM_VALUE,
    INVALID_NUMERIC,
    INVALID_PHONE,
    LEADING_WHITESPACE,
    MULTIPLE_INTERNAL_SPACES,
    TRAILING_WHITESPACE,
)
from app.remediation.actions.booleans import propose_normalize_boolean
from app.remediation.actions.capitalization import propose_standardize_capitalization
from app.remediation.actions.collapse import propose_collapse
from app.remediation.actions.dates import propose_normalize_date
from app.remediation.actions.duplicates import (
    propose_remove_duplicate_primary_key,
    propose_remove_duplicate_row,
)
from app.remediation.actions.enum_values import propose_normalize_enum_value
from app.remediation.actions.numeric import propose_normalize_numeric
from app.remediation.actions.phones import propose_normalize_phone
from app.remediation.actions.trim import propose_trim
from app.remediation.skip_reasons import (
    ALLOWED_VALUES_NOT_CONFIGURED,
    CAPITALIZATION_TARGET_NOT_CONFIGURED,
    DEFAULT_COUNTRY_NOT_CONFIGURED,
    DUPLICATE_REMOVAL_NOT_ENABLED,
    NO_DETERMINISTIC_CHANGE_AVAILABLE,
    SOURCE_DATE_FORMAT_NOT_CONFIGURED,
    TARGET_DATE_FORMAT_NOT_CONFIGURED,
)
from app.remediation.types import RemediationColumnConfig, RemediationDatasetConfigInput, RemediationIssueInput


def _issue(
    issue_type: str,
    original_value: str | None = None,
    suggested_fix: str | None = None,
    column_name: str | None = "col",
    row_number: int = 2,
) -> RemediationIssueInput:
    return RemediationIssueInput(
        issue_id=uuid.uuid4(),
        row_number=row_number,
        column_name=column_name,
        issue_type=issue_type,
        original_value=original_value,
        suggested_fix=suggested_fix,
    )


# --- trim_whitespace -------------------------------------------------------


def test_trim_proposes_the_suggested_fix_verbatim():
    issue = _issue(LEADING_WHITESPACE, original_value="  Bob", suggested_fix="Bob")
    outcome = propose_trim(issue)
    assert outcome.changed
    assert outcome.proposed_value == "Bob"


def test_trim_handles_trailing_whitespace_issue_type_identically():
    issue = _issue(TRAILING_WHITESPACE, original_value="Bob  ", suggested_fix="Bob")
    outcome = propose_trim(issue)
    assert outcome.changed
    assert outcome.proposed_value == "Bob"


def test_trim_skips_when_suggested_fix_is_missing():
    issue = _issue(LEADING_WHITESPACE, original_value="Bob", suggested_fix=None)
    outcome = propose_trim(issue)
    assert not outcome.changed
    assert outcome.skip_reason == NO_DETERMINISTIC_CHANGE_AVAILABLE


# --- collapse_multiple_spaces ----------------------------------------------


def test_collapse_proposes_the_suggested_fix_verbatim():
    issue = _issue(MULTIPLE_INTERNAL_SPACES, original_value="Bob   Jones", suggested_fix="Bob Jones")
    outcome = propose_collapse(issue)
    assert outcome.changed
    assert outcome.proposed_value == "Bob Jones"


def test_collapse_skips_when_suggested_fix_equals_original():
    issue = _issue(MULTIPLE_INTERNAL_SPACES, original_value="Bob Jones", suggested_fix="Bob Jones")
    outcome = propose_collapse(issue)
    assert not outcome.changed
    assert outcome.skip_reason == NO_DETERMINISTIC_CHANGE_AVAILABLE


# --- standardize_capitalization ---------------------------------------------


def test_capitalization_applies_configured_target():
    issue = _issue(INCONSISTENT_CAPITALIZATION, original_value="bob smith")
    outcome = propose_standardize_capitalization(
        issue, RemediationColumnConfig(capitalization_target="title")
    )
    assert outcome.changed
    assert outcome.proposed_value == "Bob Smith"


def test_capitalization_skips_without_configured_target():
    issue = _issue(INCONSISTENT_CAPITALIZATION, original_value="bob smith")
    outcome = propose_standardize_capitalization(issue, None)
    assert not outcome.changed
    assert outcome.skip_reason == CAPITALIZATION_TARGET_NOT_CONFIGURED


def test_capitalization_skips_when_value_already_matches_target():
    issue = _issue(INCONSISTENT_CAPITALIZATION, original_value="BOB SMITH")
    outcome = propose_standardize_capitalization(
        issue, RemediationColumnConfig(capitalization_target="upper")
    )
    assert not outcome.changed
    assert outcome.skip_reason == NO_DETERMINISTIC_CHANGE_AVAILABLE


def test_capitalization_title_target_avoids_str_title_apostrophe_bug():
    # Phase 5 regression: this action must reuse
    # app.standardization.rules.casing.title_case rather than Python's
    # built-in str.title(), which mangles the letter after an apostrophe
    # ("don't".title() == "Don'T"). Proves the fix stays in place.
    issue = _issue(INCONSISTENT_CAPITALIZATION, original_value="don't panic, o'brien")
    outcome = propose_standardize_capitalization(
        issue, RemediationColumnConfig(capitalization_target="title")
    )
    assert outcome.changed
    assert outcome.proposed_value == "Don't Panic, O'brien"
    assert "'T" not in outcome.proposed_value
    assert "'B" not in outcome.proposed_value


# --- normalize_boolean -------------------------------------------------------


def test_normalize_boolean_canonicalizes_recognized_token():
    issue = _issue(BOOLEAN_INCONSISTENCY, original_value="1")
    outcome = propose_normalize_boolean(issue)
    assert outcome.changed
    assert outcome.proposed_value == "true"


def test_normalize_boolean_leaves_unrecognized_token_untouched():
    issue = _issue(BOOLEAN_INCONSISTENCY, original_value="garbage")
    outcome = propose_normalize_boolean(issue)
    assert not outcome.changed
    assert outcome.skip_reason == NO_DETERMINISTIC_CHANGE_AVAILABLE


# --- normalize_date -----------------------------------------------------------


def test_normalize_date_renders_date_only_target_format():
    issue = _issue(INVALID_DATE, original_value="01/15/2024")
    outcome = propose_normalize_date(
        issue,
        RemediationColumnConfig(
            source_date_format="%m/%d/%Y", target_date_format="%Y-%m-%d"
        ),
    )
    assert outcome.changed
    assert outcome.proposed_value == "2024-01-15"


def test_normalize_date_renders_configured_datetime_target_format():
    issue = _issue(INVALID_DATE, original_value="01/15/2024")
    outcome = propose_normalize_date(
        issue,
        RemediationColumnConfig(
            source_date_format="%m/%d/%Y", target_date_format="%Y-%m-%dT%H:%M:%S"
        ),
    )
    assert outcome.changed
    # Source had no time component -- midnight appears only because the
    # configured TARGET format explicitly asked for a time component, not
    # because the action forces it.
    assert outcome.proposed_value == "2024-01-15T00:00:00"


def test_normalize_date_skips_without_configured_source_format():
    issue = _issue(INVALID_DATE, original_value="01/15/2024")
    outcome = propose_normalize_date(
        issue, RemediationColumnConfig(target_date_format="%Y-%m-%d")
    )
    assert not outcome.changed
    assert outcome.skip_reason == SOURCE_DATE_FORMAT_NOT_CONFIGURED


def test_normalize_date_skips_without_configured_target_format():
    issue = _issue(INVALID_DATE, original_value="01/15/2024")
    outcome = propose_normalize_date(
        issue, RemediationColumnConfig(source_date_format="%m/%d/%Y")
    )
    assert not outcome.changed
    assert outcome.skip_reason == TARGET_DATE_FORMAT_NOT_CONFIGURED


def test_normalize_date_skips_when_neither_format_is_configured():
    issue = _issue(INVALID_DATE, original_value="01/15/2024")
    outcome = propose_normalize_date(issue, None)
    assert not outcome.changed
    assert outcome.skip_reason == SOURCE_DATE_FORMAT_NOT_CONFIGURED


def test_normalize_date_never_guesses_a_second_source_format():
    # Value does not match the configured source format at all -- must be
    # left untouched, never retried against a different format.
    issue = _issue(INVALID_DATE, original_value="15-Jan-2024")
    outcome = propose_normalize_date(
        issue,
        RemediationColumnConfig(
            source_date_format="%m/%d/%Y", target_date_format="%Y-%m-%d"
        ),
    )
    assert not outcome.changed
    assert outcome.skip_reason == NO_DETERMINISTIC_CHANGE_AVAILABLE


def test_normalize_date_skips_when_target_rendering_equals_original_value():
    # Source already looks exactly like the configured target rendering --
    # no change to propose.
    issue = _issue(INVALID_DATE, original_value="2024-01-15")
    outcome = propose_normalize_date(
        issue,
        RemediationColumnConfig(
            source_date_format="%Y-%m-%d", target_date_format="%Y-%m-%d"
        ),
    )
    assert not outcome.changed
    assert outcome.skip_reason == NO_DETERMINISTIC_CHANGE_AVAILABLE


# --- normalize_phone ----------------------------------------------------------


def test_normalize_phone_formats_to_e164_with_configured_country():
    issue = _issue(INVALID_PHONE, original_value="4155552671")
    outcome = propose_normalize_phone(issue, RemediationColumnConfig(default_country="US"))
    assert outcome.changed
    assert outcome.proposed_value == "+14155552671"


def test_normalize_phone_skips_without_configured_country():
    issue = _issue(INVALID_PHONE, original_value="4155552671")
    outcome = propose_normalize_phone(issue, None)
    assert not outcome.changed
    assert outcome.skip_reason == DEFAULT_COUNTRY_NOT_CONFIGURED


def test_normalize_phone_leaves_unparseable_value_untouched_even_with_country():
    issue = _issue(INVALID_PHONE, original_value="not-a-phone-number")
    outcome = propose_normalize_phone(issue, RemediationColumnConfig(default_country="US"))
    assert not outcome.changed
    assert outcome.skip_reason == NO_DETERMINISTIC_CHANGE_AVAILABLE


# --- normalize_numeric ---------------------------------------------------------


def test_normalize_numeric_strips_thousands_separator():
    issue = _issue(INVALID_NUMERIC, original_value="1,234")
    outcome = propose_normalize_numeric(issue)
    assert outcome.changed
    assert outcome.proposed_value == "1234"


def test_normalize_numeric_leaves_ambiguous_value_untouched():
    # A single comma that doesn't match the thousands-grouping shape and
    # no locale configured -- genuinely ambiguous, must not guess.
    issue = _issue(INVALID_NUMERIC, original_value="12,3")
    outcome = propose_normalize_numeric(issue)
    assert not outcome.changed
    assert outcome.skip_reason == NO_DETERMINISTIC_CHANGE_AVAILABLE


# --- normalize_enum_value -----------------------------------------------------


def test_enum_value_matches_case_insensitively_to_single_candidate():
    issue = _issue(INVALID_ENUM_VALUE, original_value="ACTIVE")
    outcome = propose_normalize_enum_value(
        issue, RemediationColumnConfig(allowed_values=("active", "inactive"))
    )
    assert outcome.changed
    assert outcome.proposed_value == "active"


def test_enum_value_skips_without_configured_allowed_values():
    issue = _issue(INVALID_ENUM_VALUE, original_value="ACTIVE")
    outcome = propose_normalize_enum_value(issue, None)
    assert not outcome.changed
    assert outcome.skip_reason == ALLOWED_VALUES_NOT_CONFIGURED


def test_enum_value_skips_when_zero_case_insensitive_matches():
    issue = _issue(INVALID_ENUM_VALUE, original_value="unknown")
    outcome = propose_normalize_enum_value(
        issue, RemediationColumnConfig(allowed_values=("active", "inactive"))
    )
    assert not outcome.changed
    assert outcome.skip_reason == NO_DETERMINISTIC_CHANGE_AVAILABLE


def test_enum_value_skips_when_multiple_case_insensitive_matches():
    # Two allowed values that only differ by case -- which one was
    # "meant" is a guess, never made.
    issue = _issue(INVALID_ENUM_VALUE, original_value="active")
    outcome = propose_normalize_enum_value(
        issue, RemediationColumnConfig(allowed_values=("Active", "ACTIVE"))
    )
    assert not outcome.changed
    assert outcome.skip_reason == NO_DETERMINISTIC_CHANGE_AVAILABLE


# --- remove_duplicate_row / remove_duplicate_primary_key -----------------------


def test_remove_duplicate_row_proposes_exclusion_when_enabled():
    outcome = propose_remove_duplicate_row(
        RemediationDatasetConfigInput(remove_duplicate_rows_enabled=True)
    )
    assert outcome.changed
    assert outcome.proposed_value is None


def test_remove_duplicate_row_skips_when_not_enabled():
    outcome = propose_remove_duplicate_row(RemediationDatasetConfigInput())
    assert not outcome.changed
    assert outcome.skip_reason == DUPLICATE_REMOVAL_NOT_ENABLED


def test_remove_duplicate_primary_key_proposes_exclusion_when_enabled():
    outcome = propose_remove_duplicate_primary_key(
        RemediationDatasetConfigInput(remove_duplicate_primary_keys_enabled=True)
    )
    assert outcome.changed
    assert outcome.proposed_value is None


def test_remove_duplicate_primary_key_skips_when_not_enabled():
    outcome = propose_remove_duplicate_primary_key(RemediationDatasetConfigInput())
    assert not outcome.changed
    assert outcome.skip_reason == DUPLICATE_REMOVAL_NOT_ENABLED


def test_duplicate_row_and_duplicate_primary_key_flags_are_independent():
    cfg = RemediationDatasetConfigInput(
        remove_duplicate_rows_enabled=True, remove_duplicate_primary_keys_enabled=False
    )
    assert propose_remove_duplicate_row(cfg).changed
    assert not propose_remove_duplicate_primary_key(cfg).changed
