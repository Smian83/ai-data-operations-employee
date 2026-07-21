"""Module 7 unit tests: each standardization rule function in isolation,
mirroring test_cleaning_rules.py's shape and coverage discipline (Section
12 of the design doc). Pure -- no DB, no client. Every rule tested for
conforming input, already-standardized input (no-op), and representative
edge cases; country-dependent rules (state/province, postal code, phone
E.164) tested both with and without a resolvable country to prove the
"leave untouched when ambiguous" guarantee. All expected outputs below
were verified empirically against the actual rule implementations before
being written as assertions, not assumed."""
from app.standardization.rules import casing, constants
from app.standardization.rules.abbreviation import apply_generic_abbreviation_lookup
from app.standardization.rules.contact import standardize_email, standardize_phone
from app.standardization.rules.geo import (
    expand_address_abbreviations,
    resolve_country_code,
    standardize_city,
    standardize_country,
    standardize_postal_code,
    standardize_state_province,
)
from app.standardization.rules.lookups import DEFAULT_ADDRESS_ABBREVIATIONS
from app.standardization.rules.names import standardize_company_name, standardize_person_name
from app.standardization.rules.temporal import standardize_date, standardize_time
from app.standardization.rules.values import (
    standardize_boolean,
    standardize_currency,
    standardize_numeric,
)

# --- casing --------------------------------------------------------------


def test_title_case_capitalizes_each_space_separated_word():
    assert casing.title_case("jane doe") == "Jane Doe"


def test_title_case_lower_cases_the_rest_of_each_word():
    assert casing.title_case("JANE DOE") == "Jane Doe"


def test_title_case_step_is_noop_on_already_title_cased_value():
    assert casing.title_case_step("Jane Doe") == ("Jane Doe", None)


def test_title_case_step_is_noop_on_empty_string():
    assert casing.title_case_step("") == ("", None)


def test_title_case_step_returns_rule_name_when_changed():
    assert casing.title_case_step("jane doe") == ("Jane Doe", constants.RULE_TITLE_CASE)


# --- person_name / company_name -------------------------------------------


def test_standardize_person_name_title_cases():
    assert standardize_person_name("jane doe") == ("Jane Doe", constants.RULE_TITLE_CASE)


def test_standardize_person_name_is_noop_on_already_standardized_value():
    assert standardize_person_name("Jane Doe") == ("Jane Doe", None)


def test_standardize_company_name_applies_org_suffix_lookup():
    lookup = {"acme inc": "Acme Incorporated"}
    assert standardize_company_name("Acme Inc", lookup) == (
        "Acme Incorporated",
        constants.RULE_COMPANY_SUFFIX_LOOKUP,
    )


def test_standardize_company_name_falls_back_to_title_case_without_lookup_match():
    assert standardize_company_name("acme corp", {}) == ("Acme Corp", constants.RULE_TITLE_CASE)


def test_standardize_company_name_is_noop_on_already_canonical_lookup_value():
    lookup = {"acme incorporated": "Acme Incorporated"}
    assert standardize_company_name("Acme Incorporated", lookup) == ("Acme Incorporated", None)


def test_standardize_company_name_is_noop_on_empty_value():
    assert standardize_company_name("", {}) == ("", None)


# --- email -----------------------------------------------------------------


def test_standardize_email_lower_cases():
    assert standardize_email("John@Example.com") == ("john@example.com", constants.RULE_LOWER_CASE)


def test_standardize_email_is_noop_on_already_lower_case_value():
    assert standardize_email("john@example.com") == ("john@example.com", None)


def test_standardize_email_leaves_value_with_no_at_sign_untouched():
    assert standardize_email("not-an-email") == ("not-an-email", None)


def test_standardize_email_leaves_value_with_multiple_at_signs_untouched():
    assert standardize_email("a@b@c") == ("a@b@c", None)


def test_standardize_email_leaves_value_with_empty_local_part_untouched():
    assert standardize_email("@example.com") == ("@example.com", None)


def test_standardize_email_is_noop_on_empty_value():
    assert standardize_email("") == ("", None)


# --- phone (country-dependent) ----------------------------------------------


def test_standardize_phone_converts_to_e164_with_resolvable_country():
    assert standardize_phone("2025550123", "US") == ("+12025550123", constants.RULE_PHONE_E164)


def test_standardize_phone_is_noop_on_already_e164_value():
    result = standardize_phone("+12025550123", "US")
    assert result == ("+12025550123", None)


def test_standardize_phone_left_untouched_without_resolvable_country():
    assert standardize_phone("2025550123", None) == ("2025550123", None)


def test_standardize_phone_leaves_unparseable_value_untouched():
    assert standardize_phone("not-a-phone-number", "US") == ("not-a-phone-number", None)


def test_standardize_phone_is_noop_on_empty_value():
    assert standardize_phone("", "US") == ("", None)


# --- postal_address ----------------------------------------------------------


def test_expand_address_abbreviations_expands_known_token():
    result = expand_address_abbreviations("123 Main St", DEFAULT_ADDRESS_ABBREVIATIONS)
    assert result == ("123 Main Street", constants.RULE_ADDRESS_ABBREVIATION)


def test_expand_address_abbreviations_is_noop_on_already_expanded_value():
    result = expand_address_abbreviations("123 Main Street", DEFAULT_ADDRESS_ABBREVIATIONS)
    assert result == ("123 Main Street", None)


def test_expand_address_abbreviations_is_noop_on_empty_value():
    assert expand_address_abbreviations("", DEFAULT_ADDRESS_ABBREVIATIONS) == ("", None)


# --- city --------------------------------------------------------------------


def test_standardize_city_title_cases():
    assert standardize_city("new york") == ("New York", constants.RULE_TITLE_CASE)


def test_standardize_city_is_noop_on_already_standardized_value():
    assert standardize_city("New York") == ("New York", None)


# --- country -------------------------------------------------------------------


def test_standardize_country_normalizes_known_variant():
    assert standardize_country("USA", {}) == ("US", constants.RULE_COUNTRY_ISO_NORMALIZE)


def test_standardize_country_is_noop_on_already_canonical_code():
    assert standardize_country("US", {}) == ("US", None)


def test_standardize_country_leaves_unrecognized_value_untouched_never_guessed():
    assert standardize_country("Narnia", {}) == ("Narnia", None)


def test_standardize_country_org_lookup_takes_precedence_over_built_in_default():
    """Same key ('united states') resolves to the built-in 'US' with no
    override, but to the organization's own preferred form when one is
    configured -- proving lookup-table precedence (Section 12)."""
    assert standardize_country("United States", {}) == (
        "US",
        constants.RULE_COUNTRY_ISO_NORMALIZE,
    )
    org_lookup = {"united states": "USA"}
    assert standardize_country("United States", org_lookup) == (
        "USA",
        constants.RULE_COUNTRY_ISO_NORMALIZE,
    )


def test_standardize_country_is_noop_on_empty_value():
    assert standardize_country("", {}) == ("", None)


def test_resolve_country_code_resolves_known_variant():
    assert resolve_country_code("USA", {}) == "US"


def test_resolve_country_code_accepts_already_alpha2_value():
    assert resolve_country_code("XY", {}) == "XY"


def test_resolve_country_code_returns_none_for_unresolvable_value():
    assert resolve_country_code("Unknown Place", {}) is None


def test_resolve_country_code_returns_none_for_empty_value():
    assert resolve_country_code("", {}) is None


# --- state_province (country-dependent) ---------------------------------------


def test_standardize_state_province_normalizes_us_state_name():
    result = standardize_state_province("California", "US")
    assert result == ("CA", constants.RULE_STATE_PROVINCE_ABBREVIATION)


def test_standardize_state_province_normalizes_canadian_province_name():
    result = standardize_state_province("Ontario", "CA")
    assert result == ("ON", constants.RULE_STATE_PROVINCE_ABBREVIATION)


def test_standardize_state_province_left_untouched_without_resolvable_country():
    assert standardize_state_province("California", None) == ("California", None)


def test_standardize_state_province_left_untouched_for_unsupported_country():
    assert standardize_state_province("Bavaria", "DE") == ("Bavaria", None)


def test_standardize_state_province_is_noop_on_already_abbreviated_value():
    assert standardize_state_province("CA", "US") == ("CA", None)


# --- postal_code (country-dependent) --------------------------------------------


def test_standardize_postal_code_formats_us_zip_plus_4():
    result = standardize_postal_code("123456789", "US")
    assert result == ("12345-6789", constants.RULE_POSTAL_CODE_FORMAT)


def test_standardize_postal_code_is_noop_on_already_formatted_us_zip5():
    assert standardize_postal_code("12345", "US") == ("12345", None)


def test_standardize_postal_code_is_noop_on_already_formatted_us_zip_plus_4():
    assert standardize_postal_code("12345-6789", "US") == ("12345-6789", None)


def test_standardize_postal_code_formats_canadian_postal_code():
    result = standardize_postal_code("K1A0B1", "CA")
    assert result == ("K1A 0B1", constants.RULE_POSTAL_CODE_FORMAT)


def test_standardize_postal_code_is_noop_on_already_formatted_canadian_code():
    assert standardize_postal_code("K1A 0B1", "CA") == ("K1A 0B1", None)


def test_standardize_postal_code_left_untouched_without_resolvable_country():
    assert standardize_postal_code("12345", None) == ("12345", None)


def test_standardize_postal_code_left_untouched_for_unsupported_country():
    assert standardize_postal_code("12345", "MX") == ("12345", None)


def test_standardize_postal_code_left_untouched_on_invalid_shape():
    assert standardize_postal_code("1234", "US") == ("1234", None)


# --- date / time ---------------------------------------------------------------


def test_standardize_date_normalizes_date_only_value_to_canonical_datetime():
    result = standardize_date("2024-01-01", None)
    assert result == ("2024-01-01T00:00:00", constants.RULE_DATE_FORMAT_NORMALIZE)


def test_standardize_date_normalizes_zulu_suffix_to_offset_form():
    result = standardize_date("2024-01-01T12:00:00Z", None)
    assert result == ("2024-01-01T12:00:00+00:00", constants.RULE_DATE_FORMAT_NORMALIZE)


def test_standardize_date_is_noop_on_already_canonical_value():
    assert standardize_date("2024-01-01T00:00:00", None) == ("2024-01-01T00:00:00", None)


def test_standardize_date_leaves_unparseable_value_untouched():
    assert standardize_date("not-a-date", None) == ("not-a-date", None)


def test_standardize_date_applies_configured_output_format():
    result = standardize_date("2024-01-01", "%m/%d/%Y")
    assert result == ("01/01/2024", constants.RULE_DATE_FORMAT_NORMALIZE)


def test_standardize_date_is_noop_on_empty_value():
    assert standardize_date("", None) == ("", None)


def test_standardize_time_normalizes_missing_seconds():
    result = standardize_time("09:30")
    assert result == ("09:30:00", constants.RULE_TIME_FORMAT_NORMALIZE)


def test_standardize_time_is_noop_on_already_canonical_value():
    assert standardize_time("09:30:00") == ("09:30:00", None)


def test_standardize_time_leaves_unparseable_value_untouched():
    """Non-zero-padded hours (e.g. '9:30') and 12-hour AM/PM forms are not
    valid ISO-8601 time literals -- left untouched rather than guessed,
    same principle as every other rule's unparseable-input handling."""
    assert standardize_time("9:30") == ("9:30", None)
    assert standardize_time("9:30 AM") == ("9:30 AM", None)


def test_standardize_time_leaves_garbage_value_untouched():
    assert standardize_time("not-a-time") == ("not-a-time", None)


def test_standardize_time_is_noop_on_empty_value():
    assert standardize_time("") == ("", None)


# --- boolean -------------------------------------------------------------------


def test_standardize_boolean_normalizes_truthy_variants_to_canonical_true():
    for token in ("Yes", "1", "y", "TRUE"):
        assert standardize_boolean(token, None) == ("true", constants.RULE_BOOLEAN_FORMAT_NORMALIZE)


def test_standardize_boolean_normalizes_falsy_variants_to_canonical_false():
    for token in ("No", "0", "n", "FALSE"):
        assert standardize_boolean(token, None) == ("false", constants.RULE_BOOLEAN_FORMAT_NORMALIZE)


def test_standardize_boolean_is_noop_on_already_canonical_true():
    assert standardize_boolean("true", None) == ("true", None)


def test_standardize_boolean_applies_configured_output_form():
    assert standardize_boolean("yes", ("Y", "N")) == ("Y", constants.RULE_BOOLEAN_FORMAT_NORMALIZE)


def test_standardize_boolean_leaves_unrecognized_value_untouched():
    assert standardize_boolean("maybe", None) == ("maybe", None)


def test_standardize_boolean_is_noop_on_empty_value():
    assert standardize_boolean("", None) == ("", None)


# --- numeric ---------------------------------------------------------------------


def test_standardize_numeric_strips_us_thousands_separator():
    result = standardize_numeric("1,234.56", None)
    assert result == ("1234.56", constants.RULE_NUMERIC_FORMAT_NORMALIZE)


def test_standardize_numeric_is_noop_on_already_canonical_value():
    assert standardize_numeric("1234.56", None) == ("1234.56", None)


def test_standardize_numeric_interprets_decimal_comma_with_eu_locale():
    result = standardize_numeric("1234,56", "eu")
    assert result == ("1234.56", constants.RULE_NUMERIC_FORMAT_NORMALIZE)


def test_standardize_numeric_leaves_ambiguous_comma_untouched_without_locale():
    """'1234,56' alone (no dot) is genuinely ambiguous -- European decimal
    comma vs. an incomplete thousands grouping -- and must be left
    untouched without a configured locale, never guessed."""
    assert standardize_numeric("1234,56", None) == ("1234,56", None)


def test_standardize_numeric_leaves_ambiguous_dot_only_untouched_by_default():
    """'1.234' alone is a different number under US vs. European
    convention -- left untouched without a configured locale."""
    assert standardize_numeric("1.234", None) == ("1.234", None)


def test_standardize_numeric_strips_eu_thousands_dot_with_eu_locale():
    result = standardize_numeric("1.234", "eu")
    assert result == ("1234", constants.RULE_NUMERIC_FORMAT_NORMALIZE)


def test_standardize_numeric_resolves_mixed_separators_by_rightmost_position():
    assert standardize_numeric("1.234,56", None) == (
        "1234.56",
        constants.RULE_NUMERIC_FORMAT_NORMALIZE,
    )
    assert standardize_numeric("1,234.56", None) == (
        "1234.56",
        constants.RULE_NUMERIC_FORMAT_NORMALIZE,
    )


def test_standardize_numeric_is_noop_on_empty_value():
    assert standardize_numeric("", None) == ("", None)


# --- currency ----------------------------------------------------------------------


def test_standardize_currency_normalizes_unambiguous_symbol_to_iso4217():
    result = standardize_currency("€1234.56", None, None)
    assert result == ("1234.56 EUR", constants.RULE_CURRENCY_FORMAT_NORMALIZE)


def test_standardize_currency_normalizes_pound_symbol():
    assert standardize_currency("£100", None, None) == (
        "100 GBP",
        constants.RULE_CURRENCY_FORMAT_NORMALIZE,
    )


def test_standardize_currency_resolves_ambiguous_dollar_via_configured_default():
    result = standardize_currency("$1,234.56", "USD", None)
    assert result == ("1234.56 USD", constants.RULE_CURRENCY_FORMAT_NORMALIZE)


def test_standardize_currency_leaves_ambiguous_dollar_untouched_without_default():
    assert standardize_currency("$1234.56", None, None) == ("$1234.56", None)


def test_standardize_currency_leaves_value_with_no_recognized_symbol_untouched():
    assert standardize_currency("1234.56", None, None) == ("1234.56", None)


def test_standardize_currency_is_noop_on_empty_value():
    assert standardize_currency("", None, None) == ("", None)


# --- generic abbreviation lookup ------------------------------------------------


def test_apply_generic_abbreviation_lookup_applies_configured_entry():
    result = apply_generic_abbreviation_lookup("Inc", {"inc": "Incorporated"})
    assert result == ("Incorporated", constants.RULE_ABBREVIATION_LOOKUP)


def test_apply_generic_abbreviation_lookup_is_noop_without_matching_entry():
    assert apply_generic_abbreviation_lookup("Inc", {}) == ("Inc", None)


def test_apply_generic_abbreviation_lookup_is_noop_on_empty_value():
    assert apply_generic_abbreviation_lookup("", {"inc": "Incorporated"}) == ("", None)


# --- RULE_CONFIDENCE / RULE_REASONS completeness --------------------------------


def test_every_module7_rule_constant_has_a_confidence_and_reason():
    rule_names = {
        constants.RULE_TRIM_WHITESPACE,
        constants.RULE_TITLE_CASE,
        constants.RULE_UPPER_CASE,
        constants.RULE_LOWER_CASE,
        constants.RULE_COMPANY_SUFFIX_LOOKUP,
        constants.RULE_PHONE_E164,
        constants.RULE_ADDRESS_ABBREVIATION,
        constants.RULE_STATE_PROVINCE_ABBREVIATION,
        constants.RULE_COUNTRY_ISO_NORMALIZE,
        constants.RULE_POSTAL_CODE_FORMAT,
        constants.RULE_DATE_FORMAT_NORMALIZE,
        constants.RULE_TIME_FORMAT_NORMALIZE,
        constants.RULE_BOOLEAN_FORMAT_NORMALIZE,
        constants.RULE_NUMERIC_FORMAT_NORMALIZE,
        constants.RULE_CURRENCY_FORMAT_NORMALIZE,
        constants.RULE_ABBREVIATION_LOOKUP,
    }
    assert rule_names == set(constants.RULE_CONFIDENCE.keys())
    assert rule_names == set(constants.RULE_REASONS.keys())
    for confidence in constants.RULE_CONFIDENCE.values():
        assert 0.0 < confidence <= 1.0
