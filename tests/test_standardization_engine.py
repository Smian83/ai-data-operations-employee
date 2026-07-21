"""Module 7 unit tests for app.standardization.engine.standardize(). Pure
-- no DB, no client. Covers: field-type dispatch and rule ordering
(whitespace trim first, universally), confidence aggregation including
the zero-changes case, the persisted-changes cap, changes_by_rule
accounting, per-row country resolution feeding country-dependent rules,
the postal_address two-step (abbreviation then casing), the generic
abbreviation pass on free-text field types, unclassified columns left
alone, engine-level determinism (Section 12), and engine-level
idempotency (Section 11's added acceptance criterion: standardizing
already-standardized output produces zero further changes). All expected
outputs below were verified empirically against the actual engine before
being written as assertions, not assumed."""
from app.standardization.engine import STANDARDIZATION_ENGINE_VERSION, standardize
from app.standardization.rules import constants
from app.standardization.types import LookupTables, StandardizationConfig, StandardizationLimits


def _limits(max_persisted_changes: int = 10_000) -> StandardizationLimits:
    return StandardizationLimits(max_persisted_changes=max_persisted_changes)


def test_standardize_applies_no_changes_to_already_standardized_uniform_data():
    headers = ["name", "email"]
    rows = [["Jane Doe", "jane@example.com"]]
    field_types = ["person_name", "email"]
    result = standardize(rows, headers, field_types, LookupTables(), StandardizationConfig(), _limits())

    assert result.standardized_rows == rows
    assert result.changes == []
    assert result.total_changes_count == 0
    assert result.changes_by_rule == {}
    # Zero-changes case: confidence defaults to 1.0, not undefined/zero.
    assert result.confidence_score == 1.0


def test_standardize_trims_whitespace_before_the_field_type_rule_runs():
    headers = ["name"]
    rows = [["  jane doe  "]]
    result = standardize(
        rows, headers, ["person_name"], LookupTables(), StandardizationConfig(), _limits()
    )

    assert result.standardized_rows == [["Jane Doe"]]
    rule_names = [c.rule_name for c in result.changes]
    assert constants.RULE_TRIM_WHITESPACE in rule_names
    assert constants.RULE_TITLE_CASE in rule_names
    assert rule_names.index(constants.RULE_TRIM_WHITESPACE) < rule_names.index(
        constants.RULE_TITLE_CASE
    )


def test_standardize_leaves_unclassified_column_completely_untouched():
    headers = ["mystery"]
    rows = [["  raw value  "]]
    result = standardize(rows, headers, [None], LookupTables(), StandardizationConfig(), _limits())

    assert result.standardized_rows == rows
    assert result.changes == []


def test_standardize_records_every_change_with_a_non_null_field_type_rule_reason_and_confidence():
    headers = ["name"]
    rows = [["  jane doe  "]]
    result = standardize(
        rows, headers, ["person_name"], LookupTables(), StandardizationConfig(), _limits()
    )

    assert len(result.changes) == 2
    for change in result.changes:
        assert change.field_type == "person_name"
        assert change.rule_name is not None
        assert change.reason
        assert 0.0 < change.confidence <= 1.0
        assert change.rule_version == STANDARDIZATION_ENGINE_VERSION


def test_standardize_postal_address_applies_abbreviation_then_casing_as_two_steps():
    headers = ["address"]
    rows = [["123 main st"]]
    result = standardize(
        rows, headers, ["postal_address"], LookupTables(), StandardizationConfig(), _limits()
    )

    assert result.standardized_rows == [["123 Main Street"]]
    rule_names = [c.rule_name for c in result.changes]
    assert constants.RULE_ADDRESS_ABBREVIATION in rule_names
    assert constants.RULE_TITLE_CASE in rule_names
    assert rule_names.index(constants.RULE_ADDRESS_ABBREVIATION) < rule_names.index(
        constants.RULE_TITLE_CASE
    )


def test_standardize_resolves_country_first_and_feeds_dependent_rules_on_the_same_row():
    """phone/state_province/postal_code all depend on the row's resolved
    country -- proven here by standardizing all four together and
    confirming the country-dependent rules actually fired."""
    headers = ["country", "phone", "state", "zip"]
    rows = [["USA", "2025550123", "California", "123456789"]]
    field_types = ["country", "phone", "state_province", "postal_code"]
    result = standardize(rows, headers, field_types, LookupTables(), StandardizationConfig(), _limits())

    assert result.standardized_rows == [["US", "+12025550123", "CA", "12345-6789"]]
    rule_names = {c.rule_name for c in result.changes}
    assert constants.RULE_COUNTRY_ISO_NORMALIZE in rule_names
    assert constants.RULE_PHONE_E164 in rule_names
    assert constants.RULE_STATE_PROVINCE_ABBREVIATION in rule_names
    assert constants.RULE_POSTAL_CODE_FORMAT in rule_names


def test_standardize_leaves_country_dependent_rules_untouched_without_a_country_column():
    headers = ["phone", "state", "zip"]
    rows = [["2025550123", "California", "123456789"]]
    field_types = ["phone", "state_province", "postal_code"]
    result = standardize(rows, headers, field_types, LookupTables(), StandardizationConfig(), _limits())

    # Only whitespace-trim could apply here (there is none to trim), so
    # the country-dependent rules must not have fired at all.
    assert result.standardized_rows == rows
    assert result.changes == []


def test_standardize_uses_configured_default_country_when_no_country_column_present():
    headers = ["phone"]
    rows = [["2025550123"]]
    config = StandardizationConfig(default_country="US")
    result = standardize(rows, headers, ["phone"], LookupTables(), config, _limits())

    assert result.standardized_rows == [["+12025550123"]]


def test_standardize_applies_generic_abbreviation_lookup_after_field_type_rule():
    """The generic (field_type=NULL) abbreviation pass only applies to the
    free-text field types (person_name, company_name, city, state_province,
    country) and runs AFTER that field's own type-specific rule -- proven
    here since the lookup key ('nyc') only matches after the value has
    already been through standardize_city's title-casing... no, the
    generic lookup matches case-insensitively regardless, but the recorded
    change must still show it as a distinct, later step."""
    headers = ["city"]
    rows = [["nyc"]]
    lookup = LookupTables(scoped={}, global_={"nyc": "New York City"})
    result = standardize(rows, headers, ["city"], lookup, StandardizationConfig(), _limits())

    assert result.standardized_rows == [["New York City"]]
    rule_names = [c.rule_name for c in result.changes]
    assert constants.RULE_TITLE_CASE in rule_names
    assert constants.RULE_ABBREVIATION_LOOKUP in rule_names
    assert rule_names.index(constants.RULE_TITLE_CASE) < rule_names.index(
        constants.RULE_ABBREVIATION_LOOKUP
    )


def test_standardize_does_not_apply_generic_abbreviation_lookup_to_non_free_text_field_types():
    """numeric/date/boolean/etc. have their own strict canonical forms --
    the generic lookup pass must never touch them, even if a matching
    global lookup key exists."""
    headers = ["amount"]
    rows = [["42"]]
    lookup = LookupTables(scoped={}, global_={"42": "forty-two"})
    result = standardize(rows, headers, ["numeric"], lookup, StandardizationConfig(), _limits())

    assert result.standardized_rows == [["42"]]
    assert result.changes == []


def test_standardize_confidence_score_is_minimum_across_applied_changes_not_average():
    headers = ["name", "address"]
    rows = [["  jane doe  ", "123 main st"]]
    field_types = ["person_name", "postal_address"]
    result = standardize(rows, headers, field_types, LookupTables(), StandardizationConfig(), _limits())

    lowest = min(c.confidence for c in result.changes)
    assert result.confidence_score == lowest
    assert result.confidence_score == constants.RULE_CONFIDENCE[constants.RULE_ADDRESS_ABBREVIATION]


def test_standardize_changes_by_rule_counts_match_individual_change_rule_names():
    headers = ["name1", "name2"]
    rows = [["  jane doe  ", "  john smith  "]]
    result = standardize(
        rows, headers, ["person_name", "person_name"], LookupTables(), StandardizationConfig(), _limits()
    )

    title_case_count = sum(1 for c in result.changes if c.rule_name == constants.RULE_TITLE_CASE)
    assert result.changes_by_rule[constants.RULE_TITLE_CASE] == title_case_count
    assert result.total_changes_count == len(result.changes)


def test_standardize_caps_persisted_changes_but_keeps_total_count_accurate():
    headers = ["a", "b", "c"]
    rows = [["  x  ", "  y  ", "  z  "]]
    field_types = ["person_name", "person_name", "person_name"]
    result = standardize(
        rows, headers, field_types, LookupTables(), StandardizationConfig(),
        _limits(max_persisted_changes=1),
    )

    assert len(result.changes) == 1
    # Each cell produces two changes (trim, then title-case) -> 3 cells * 2 = 6.
    assert result.total_changes_count == 6
    assert result.changes_by_rule[constants.RULE_TRIM_WHITESPACE] == 3


def test_standardize_is_deterministic_across_repeated_runs_on_identical_input():
    headers = ["name", "company", "email", "country", "state", "zip", "address"]
    rows = [
        ["  jane doe  ", "acme inc", "Jane@Example.com", "USA", "California", "123456789", "123 Main St"],
        ["bob jones", "widgets llc", "bob@test.com", "Canada", "Ontario", "K1A0B1", "456 Oak Ave"],
    ]
    field_types = [
        "person_name", "company_name", "email", "country", "state_province",
        "postal_code", "postal_address",
    ]
    lookup = LookupTables(scoped={"company_name": {"acme inc": "Acme Incorporated"}}, global_={})
    config = StandardizationConfig()

    first = standardize(rows, headers, field_types, lookup, config, _limits())
    second = standardize(rows, headers, field_types, lookup, config, _limits())

    assert first.standardized_rows == second.standardized_rows
    assert first.total_changes_count == second.total_changes_count
    assert first.changes_by_rule == second.changes_by_rule
    assert first.confidence_score == second.confidence_score
    assert [
        (c.row_index, c.column_name, c.original_value, c.standardized_value, c.rule_name, c.confidence)
        for c in first.changes
    ] == [
        (c.row_index, c.column_name, c.original_value, c.standardized_value, c.rule_name, c.confidence)
        for c in second.changes
    ]


def test_standardize_is_idempotent_second_pass_over_own_output_produces_zero_changes():
    """The added Module 7 acceptance criterion (design doc Section 11):
    Clean -> Standardize -> Standardize again must produce zero additional
    changes, and the second run's output must be byte-identical to the
    first's. Exercised here at the engine level across every field type
    this test suite covers, feeding the first pass's own standardized_rows
    back in under the identical classification/config/lookup inputs."""
    headers = [
        "name", "company", "email", "phone", "country", "state", "zip",
        "address", "city", "date", "time", "active", "amount", "price",
    ]
    rows = [
        [
            "  jane doe  ", "acme inc", "Jane@Example.com", "2025550123",
            "USA", "California", "123456789", "123 main st", "new york",
            "2024-01-01", "9:30:00", "Yes", "1,234.56", "$1,234.56",
        ],
    ]
    field_types = [
        "person_name", "company_name", "email", "phone", "country",
        "state_province", "postal_code", "postal_address", "city", "date",
        "time", "boolean", "numeric", "currency",
    ]
    lookup = LookupTables(scoped={"company_name": {"acme inc": "Acme Incorporated"}}, global_={})
    config = StandardizationConfig(default_currency="USD")
    limits = _limits()

    first = standardize(rows, headers, field_types, lookup, config, limits)
    assert first.total_changes_count > 0  # sanity: the fixture actually exercises rules

    second = standardize(first.standardized_rows, headers, field_types, lookup, config, limits)

    assert second.total_changes_count == 0
    assert second.changes == []
    assert second.changes_by_rule == {}
    assert second.confidence_score == 1.0
    assert second.standardized_rows == first.standardized_rows


def test_standardize_engine_version_is_recorded_as_the_rule_version_on_every_change():
    headers = ["name"]
    rows = [["jane doe"]]
    result = standardize(
        rows, headers, ["person_name"], LookupTables(), StandardizationConfig(), _limits()
    )
    assert len(result.changes) == 1
    assert result.changes[0].rule_version == STANDARDIZATION_ENGINE_VERSION
