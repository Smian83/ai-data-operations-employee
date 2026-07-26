"""Module 14 Phase 2 unit tests for app.detection.validators -- the pure
predicates every format rule in app.detection.rules.formats/boolean
depends on. Pure -- no DB, no client."""
from app.detection import validators


# --- is_valid_email_syntax -------------------------------------------------------


def test_email_syntax_accepts_conforming_address():
    assert validators.is_valid_email_syntax("user@example.com") is True


def test_email_syntax_rejects_missing_at_sign():
    assert validators.is_valid_email_syntax("userexample.com") is False


def test_email_syntax_rejects_missing_domain_dot():
    assert validators.is_valid_email_syntax("user@example") is False


def test_email_syntax_rejects_empty_local_or_domain_part():
    assert validators.is_valid_email_syntax("@example.com") is False
    assert validators.is_valid_email_syntax("user@") is False


# --- is_valid_phone_international -------------------------------------------------


def test_phone_international_accepts_valid_number_with_leading_plus():
    assert validators.is_valid_phone_international("+14155552671") is True


def test_phone_international_rejects_number_without_leading_plus():
    assert validators.is_valid_phone_international("14155552671") is False


def test_phone_international_rejects_unparseable_garbage():
    assert validators.is_valid_phone_international("+not-a-number") is False


# --- is_valid_iso_date -------------------------------------------------------------


def test_iso_date_accepts_date_only_form():
    assert validators.is_valid_iso_date("2024-01-15") is True


def test_iso_date_accepts_datetime_with_z_suffix():
    assert validators.is_valid_iso_date("2024-01-15T10:30:00Z") is True


def test_iso_date_rejects_non_iso_format():
    assert validators.is_valid_iso_date("01/15/2024") is False


# --- is_valid_numeric ----------------------------------------------------------------


def test_numeric_accepts_plain_integer_and_decimal():
    assert validators.is_valid_numeric("42") is True
    assert validators.is_valid_numeric("42.5") is True


def test_numeric_accepts_thousands_comma_grouping():
    assert validators.is_valid_numeric("1,234,567") is True


def test_numeric_rejects_malformed_comma_grouping():
    assert validators.is_valid_numeric("1,23,4") is False


def test_numeric_rejects_non_numeric_word():
    assert validators.is_valid_numeric("forty") is False


def test_numeric_rejects_empty_or_blank_string():
    assert validators.is_valid_numeric("") is False
    assert validators.is_valid_numeric("   ") is False


# --- classify_boolean_token -----------------------------------------------------------


def test_classify_boolean_token_recognizes_every_family():
    assert validators.classify_boolean_token("true") == ("true_false", True)
    assert validators.classify_boolean_token("False") == ("true_false", False)
    assert validators.classify_boolean_token("YES") == ("yes_no", True)
    assert validators.classify_boolean_token("no") == ("yes_no", False)
    assert validators.classify_boolean_token("y") == ("y_n", True)
    assert validators.classify_boolean_token("N") == ("y_n", False)
    assert validators.classify_boolean_token("1") == ("one_zero", True)
    assert validators.classify_boolean_token("0") == ("one_zero", False)


def test_classify_boolean_token_returns_none_for_unrecognized_value():
    assert validators.classify_boolean_token("maybe") == (None, None)
