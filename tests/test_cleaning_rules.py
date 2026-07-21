"""Module 6 unit tests: each cleaning rule function in isolation. Pure --
no DB, no client, no fixtures beyond plain strings. Verifies conforming
input, already-clean input (no-op), and representative edge cases for
every rule in app.cleaning.rules."""
from app.cleaning import rules


# --- trim_whitespace ---------------------------------------------------------


def test_trim_whitespace_strips_leading_and_trailing_space():
    assert rules.trim_whitespace("  hello  ") == ("hello", rules.RULE_TRIM_WHITESPACE)


def test_trim_whitespace_collapses_repeated_internal_whitespace():
    assert rules.trim_whitespace("a   b") == ("a b", rules.RULE_TRIM_WHITESPACE)


def test_trim_whitespace_is_noop_on_already_clean_value():
    assert rules.trim_whitespace("hello") == ("hello", None)


def test_trim_whitespace_is_noop_on_empty_string():
    assert rules.trim_whitespace("") == ("", None)


# --- normalize_blank ---------------------------------------------------------


def test_normalize_blank_is_noop_on_already_empty_string():
    assert rules.normalize_blank("") == ("", None)


def test_normalize_blank_canonicalizes_known_blank_equivalents():
    for token in ("N/A", "n/a", "NULL", "-", "none", "NA"):
        assert rules.normalize_blank(token) == ("", rules.RULE_NORMALIZE_BLANK)


def test_normalize_blank_is_case_insensitive():
    assert rules.normalize_blank("None") == ("", rules.RULE_NORMALIZE_BLANK)
    assert rules.normalize_blank("Null") == ("", rules.RULE_NORMALIZE_BLANK)


def test_normalize_blank_leaves_ordinary_value_untouched():
    assert rules.normalize_blank("hello") == ("hello", None)


# --- coerce_type: dispatch and non-concrete types ----------------------------


def test_coerce_type_is_noop_for_non_concrete_inferred_types():
    for inferred_type in ("string", "mixed", "null"):
        assert rules.coerce_type("hello", inferred_type) == ("hello", None)


def test_coerce_type_is_noop_for_empty_value_regardless_of_type():
    for inferred_type in ("integer", "decimal", "boolean", "date", "datetime"):
        assert rules.coerce_type("", inferred_type) == ("", None)


# --- coerce_type: integer -----------------------------------------------------


def test_coerce_type_integer_is_noop_on_canonical_integer():
    assert rules.coerce_type("42", "integer") == ("42", None)


def test_coerce_type_integer_normalizes_integral_float_string():
    assert rules.coerce_type("42.0", "integer") == ("42", rules.RULE_COERCE_INTEGER)


def test_coerce_type_integer_leaves_non_numeric_value_untouched():
    assert rules.coerce_type("abc", "integer") == ("abc", None)


def test_coerce_type_integer_leaves_non_integral_decimal_untouched():
    assert rules.coerce_type("42.5", "integer") == ("42.5", None)


# --- coerce_type: decimal -----------------------------------------------------


def test_coerce_type_decimal_normalizes_trailing_zeros():
    assert rules.coerce_type("3.140", "decimal") == ("3.14", rules.RULE_COERCE_DECIMAL)


def test_coerce_type_decimal_is_noop_on_already_canonical_value():
    assert rules.coerce_type("3.14", "decimal") == ("3.14", None)


def test_coerce_type_decimal_leaves_non_numeric_value_untouched():
    assert rules.coerce_type("abc", "decimal") == ("abc", None)


# --- coerce_type: boolean ------------------------------------------------------


def test_coerce_type_boolean_normalizes_truthy_variants():
    for token in ("YES", "yes", "Y", "1", "True"):
        assert rules.coerce_type(token, "boolean") == ("true", rules.RULE_COERCE_BOOLEAN)


def test_coerce_type_boolean_normalizes_falsy_variants():
    for token in ("NO", "no", "N", "0", "False"):
        assert rules.coerce_type(token, "boolean") == ("false", rules.RULE_COERCE_BOOLEAN)


def test_coerce_type_boolean_is_noop_on_canonical_true():
    assert rules.coerce_type("true", "boolean") == ("true", None)


def test_coerce_type_boolean_is_noop_on_canonical_false():
    assert rules.coerce_type("false", "boolean") == ("false", None)


def test_coerce_type_boolean_leaves_unrecognized_value_untouched():
    assert rules.coerce_type("maybe", "boolean") == ("maybe", None)


# --- coerce_type: date / datetime ---------------------------------------------


def test_coerce_type_datetime_normalizes_zulu_suffix_to_offset_form():
    result = rules.coerce_type("2024-01-01T00:00:00Z", "datetime")
    assert result == ("2024-01-01T00:00:00+00:00", rules.RULE_NORMALIZE_DATE)


def test_coerce_type_date_reparses_date_only_value():
    result = rules.coerce_type("2024-01-01", "date")
    assert result == ("2024-01-01T00:00:00", rules.RULE_NORMALIZE_DATE)


def test_coerce_type_date_is_noop_on_already_canonical_datetime():
    assert rules.coerce_type("2024-01-01T00:00:00", "datetime") == ("2024-01-01T00:00:00", None)


def test_coerce_type_date_leaves_unparseable_value_untouched():
    assert rules.coerce_type("not-a-date", "date") == ("not-a-date", None)


# --- RULE_CONFIDENCE / RULE_REASONS completeness -------------------------------


def test_every_rule_constant_has_a_confidence_and_reason():
    rule_names = {
        rules.RULE_TRIM_WHITESPACE,
        rules.RULE_NORMALIZE_BLANK,
        rules.RULE_COERCE_INTEGER,
        rules.RULE_COERCE_DECIMAL,
        rules.RULE_COERCE_BOOLEAN,
        rules.RULE_NORMALIZE_DATE,
    }
    assert rule_names == set(rules.RULE_CONFIDENCE.keys())
    assert rule_names == set(rules.RULE_REASONS.keys())
    for confidence in rules.RULE_CONFIDENCE.values():
        assert 0.0 < confidence <= 1.0


def test_trim_whitespace_confidence_is_highest_at_one():
    assert rules.RULE_CONFIDENCE[rules.RULE_TRIM_WHITESPACE] == 1.0


def test_date_normalization_confidence_is_the_lowest():
    lowest = min(rules.RULE_CONFIDENCE.values())
    assert rules.RULE_CONFIDENCE[rules.RULE_NORMALIZE_DATE] == lowest
