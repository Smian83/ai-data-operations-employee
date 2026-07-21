"""Module 6 unit tests for app.cleaning.engine.clean(). Pure -- no DB, no
client. Covers: correct rule ordering (a value needing trim-then-coerce
fails if steps run out of order), confidence aggregation including the
zero-changes case, the persisted-changes cap, changes_by_rule accounting,
duplicate detection on cleaned output, and determinism (same input always
produces an identical result)."""
from app.cleaning import rules
from app.cleaning.engine import clean
from app.cleaning.types import CleaningLimits


def _limits(max_persisted_changes: int = 10_000) -> CleaningLimits:
    return CleaningLimits(max_persisted_changes=max_persisted_changes)


def test_clean_applies_no_changes_to_already_clean_uniform_data():
    headers = ["id", "name"]
    rows = [["1", "Ada"], ["2", "Grace"]]
    result = clean(rows, headers, column_types=["integer", "string"], limits=_limits())

    assert result.cleaned_rows == rows
    assert result.changes == []
    assert result.total_changes_count == 0
    assert result.changes_by_rule == {}
    assert result.duplicate_row_count == 0
    # Zero-changes case: confidence defaults to 1.0, not undefined/zero.
    assert result.confidence_score == 1.0


def test_clean_trims_whitespace_before_type_coercion_runs():
    """A value needing trim-then-coerce ('  42.0  ' -> integer 42) must
    pass through both stages in order. If trim ran after coercion instead,
    the leading/trailing whitespace would make integer coercion fail
    entirely (Decimal('  42.0  ') still parses fine, so this specific case
    wouldn't catch a swap -- the real proof is that the FINAL cleaned value
    has no whitespace AND is coerced, which only happens if trim runs
    first and coercion sees the already-trimmed value)."""
    headers = ["amount"]
    rows = [["  42.0  "]]
    result = clean(rows, headers, column_types=["integer"], limits=_limits())

    assert result.cleaned_rows == [["42"]]
    rule_names = [c.rule_name for c in result.changes]
    assert rules.RULE_TRIM_WHITESPACE in rule_names
    assert rules.RULE_COERCE_INTEGER in rule_names
    # trim must be recorded before coercion, proving the pipeline order.
    assert rule_names.index(rules.RULE_TRIM_WHITESPACE) < rule_names.index(
        rules.RULE_COERCE_INTEGER
    )


def test_clean_normalizes_blank_before_type_coercion_so_blanks_are_never_coerced():
    """'N/A' in an integer column must become '' (normalize_blank), and
    must NOT then be run through integer coercion (coerce_type is a no-op
    on ''). If blank-normalization ran after coercion, 'N/A' would instead
    be left completely untouched since it isn't valid Decimal input --
    the observable difference is whether the cell ends up '' or 'N/A'."""
    headers = ["amount"]
    rows = [["N/A"]]
    result = clean(rows, headers, column_types=["integer"], limits=_limits())

    assert result.cleaned_rows == [[""]]
    assert [c.rule_name for c in result.changes] == [rules.RULE_NORMALIZE_BLANK]


def test_clean_confidence_score_is_minimum_across_applied_changes_not_average():
    """One low-confidence date-normalization change alongside several
    high-confidence whitespace-trim changes must pull the run's overall
    confidence down to the minimum, not dilute it via averaging."""
    headers = ["a", "b", "created_at"]
    rows = [["  x  ", "  y  ", "2024-01-01"]]
    result = clean(
        rows, headers, column_types=["string", "string", "date"], limits=_limits()
    )

    assert rules.RULE_NORMALIZE_DATE in {c.rule_name for c in result.changes}
    assert result.confidence_score == rules.RULE_CONFIDENCE[rules.RULE_NORMALIZE_DATE]
    assert result.confidence_score < rules.RULE_CONFIDENCE[rules.RULE_TRIM_WHITESPACE]


def test_clean_changes_by_rule_counts_match_individual_change_rule_names():
    headers = ["a", "b"]
    rows = [["  x  ", "  y  "], ["  z  ", "w"]]
    result = clean(rows, headers, column_types=["string", "string"], limits=_limits())

    trim_count = sum(1 for c in result.changes if c.rule_name == rules.RULE_TRIM_WHITESPACE)
    assert result.changes_by_rule[rules.RULE_TRIM_WHITESPACE] == trim_count
    assert result.total_changes_count == len(result.changes)


def test_clean_caps_persisted_changes_but_keeps_total_count_accurate():
    """With max_persisted_changes=1, only one Change object is kept in the
    result even though multiple cells actually changed -- total_changes_
    count and changes_by_rule must still reflect the TRUE total, never the
    capped list length, so nothing is silently lost from the aggregate."""
    headers = ["a", "b", "c"]
    rows = [["  x  ", "  y  ", "  z  "]]
    result = clean(rows, headers, column_types=["string"] * 3, limits=_limits(max_persisted_changes=1))

    assert len(result.changes) == 1
    assert result.total_changes_count == 3
    assert result.changes_by_rule[rules.RULE_TRIM_WHITESPACE] == 3


def test_clean_detects_duplicate_rows_on_cleaned_output_without_removing_them():
    headers = ["id", "name"]
    # These become identical only AFTER cleaning (whitespace differs raw).
    rows = [["1", "Ada"], ["1", "Ada  "], ["2", "Grace"]]
    result = clean(rows, headers, column_types=["integer", "string"], limits=_limits())

    assert len(result.cleaned_rows) == 3  # never auto-removed
    assert result.duplicate_row_count == 1


def test_clean_leaves_mixed_and_string_columns_uncoerced():
    headers = ["note"]
    rows = [["123abc"]]
    result = clean(rows, headers, column_types=["mixed"], limits=_limits())

    assert result.cleaned_rows == [["123abc"]]
    assert result.changes == []


def test_clean_is_deterministic_across_repeated_runs_on_identical_input():
    headers = ["id", "amount", "flag", "note", "created_at"]
    rows = [
        ["1", "  42.0  ", "YES", "N/A", "2024-01-01T00:00:00Z"],
        ["2", "3.140", "no", "hello", "2024-06-15"],
        ["1", "42", "true", "", "2024-01-01T00:00:00"],
    ]
    column_types = ["integer", "decimal", "boolean", "string", "datetime"]

    first = clean(rows, headers, column_types, limits=_limits())
    second = clean(rows, headers, column_types, limits=_limits())

    assert first.cleaned_rows == second.cleaned_rows
    assert first.total_changes_count == second.total_changes_count
    assert first.changes_by_rule == second.changes_by_rule
    assert first.duplicate_row_count == second.duplicate_row_count
    assert first.confidence_score == second.confidence_score
    assert [
        (c.row_index, c.column_name, c.original_value, c.cleaned_value, c.rule_name, c.confidence)
        for c in first.changes
    ] == [
        (c.row_index, c.column_name, c.original_value, c.cleaned_value, c.rule_name, c.confidence)
        for c in second.changes
    ]
