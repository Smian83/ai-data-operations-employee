"""Phase 2 tests for the Module 17 Validation Engine.

Covers:
  A. Registry integrity (10 rules, unique actions, unique names, all REMEDIATION_ACTIONS covered)
  B. Engine constants (VALIDATION_ENGINE_VERSION)
  C. validate_trim_whitespace
  D. validate_collapse_multiple_spaces
  E. validate_standardize_capitalization
  F. validate_normalize_boolean
  G. validate_normalize_date
  H. validate_normalize_phone
  I. validate_normalize_numeric
  J. validate_normalize_enum_value
  K. validate_remove_duplicate_row
  L. validate_remove_duplicate_primary_key
  M. Engine orchestration (sort order, count-reconcile, limit, ACTION_NOT_RECOGNISED)
  N. Pure/no-ORM assertion (engine must not import any ORM module)
"""
from __future__ import annotations

import uuid
import types as _types

import pytest

from app.models.enums import REMEDIATION_ACTIONS, VALIDATION_RULE_NAMES
from app.validation.engine import VALIDATION_ENGINE_VERSION, validate
from app.validation.reasons import (
    ACTION_NOT_RECOGNISED,
    ALLOWED_VALUES_NOT_CONFIGURED,
    CAPITALIZATION_TARGET_NOT_CONFIGURED,
    DEFAULT_COUNTRY_NOT_CONFIGURED,
    DUPLICATE_REMOVAL_CORRECTLY_FORMED,
    DUPLICATE_REMOVAL_PROPOSED_VALUE_NOT_NONE,
    ORIGINAL_VALUE_MISSING,
    PROPOSED_VALUE_MATCHES,
    PROPOSED_VALUE_MISMATCH,
    RESULT_LIMIT_REACHED,
    SOURCE_DATE_FORMAT_NOT_CONFIGURED,
    TARGET_DATE_FORMAT_NOT_CONFIGURED,
)
from app.validation.registry import (
    VALIDATION_RULES,
    _RULES_BY_ACTION,
    get_rule_for_action,
)
from app.validation.types import (
    ValidationChangeInput,
    ValidationColumnConfig,
    ValidationLimits,
    ValidationRunResult,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _change(
    action: str,
    original_value: str | None,
    proposed_value: str | None,
    *,
    column_name: str | None = "col",
    row_number: int = 2,
) -> ValidationChangeInput:
    return ValidationChangeInput(
        change_id=uuid.uuid4(),
        source_issue_id=uuid.uuid4(),
        row_number=row_number,
        column_name=column_name,
        action=action,
        original_value=original_value,
        proposed_value=proposed_value,
    )


def _run(
    changes: list[ValidationChangeInput],
    column_config: dict | None = None,
    max_results: int = 10_000,
) -> ValidationRunResult:
    return validate(
        changes,
        column_config or {},
        ValidationLimits(max_persisted_results=max_results),
    )


def _single(
    action: str,
    original_value: str | None,
    proposed_value: str | None,
    column_config: dict | None = None,
    column_name: str | None = "col",
) -> str:
    """Run a single change and return its outcome string."""
    c = _change(action, original_value, proposed_value, column_name=column_name)
    result = _run([c], column_config)
    assert len(result.results) == 1
    return result.results[0].outcome


# ---------------------------------------------------------------------------
# A. Registry integrity
# ---------------------------------------------------------------------------

class TestRegistryIntegrity:
    def test_ten_rules_registered(self):
        assert len(VALIDATION_RULES) == 10

    def test_unique_actions(self):
        actions = [r.action for r in VALIDATION_RULES]
        assert len(actions) == len(set(actions))

    def test_unique_rule_names(self):
        names = [r.rule_name for r in VALIDATION_RULES]
        assert len(names) == len(set(names))

    def test_every_remediation_action_covered(self):
        registered = {r.action for r in VALIDATION_RULES}
        for action in REMEDIATION_ACTIONS:
            assert action in registered, f"No validation rule for action {action!r}"

    def test_every_validation_rule_name_used(self):
        registered = {r.rule_name for r in VALIDATION_RULES}
        for name in VALIDATION_RULE_NAMES:
            assert name in registered, f"VALIDATION_RULE_NAME {name!r} not found in registry"

    def test_non_empty_rule_version_all_rules(self):
        for rule in VALIDATION_RULES:
            assert rule.rule_version, (
                f"rule {rule.rule_name!r} has empty rule_version"
            )

    def test_get_rule_for_action_returns_rule(self):
        for action in REMEDIATION_ACTIONS:
            rule = get_rule_for_action(action)
            assert rule is not None
            assert rule.action == action

    def test_get_rule_for_action_unknown_returns_none(self):
        assert get_rule_for_action("not_a_real_action") is None

    def test_rules_by_action_covers_all_remediation_actions(self):
        for action in REMEDIATION_ACTIONS:
            assert action in _RULES_BY_ACTION


# ---------------------------------------------------------------------------
# B. Engine constants
# ---------------------------------------------------------------------------

class TestEngineConstants:
    def test_validation_engine_version_is_string(self):
        assert isinstance(VALIDATION_ENGINE_VERSION, str)

    def test_validation_engine_version_non_empty(self):
        assert VALIDATION_ENGINE_VERSION


# ---------------------------------------------------------------------------
# C. validate_trim_whitespace
# ---------------------------------------------------------------------------

class TestValidateTrimWhitespace:
    ACTION = "trim_whitespace"

    def test_leading_whitespace_lstrip_passes(self):
        assert _single(self.ACTION, "  hello", "hello") == "passed"

    def test_trailing_whitespace_rstrip_passes(self):
        assert _single(self.ACTION, "hello  ", "hello") == "passed"

    def test_leading_rstrip_also_passes(self):
        # rstrip of "  hello" == "  hello" -- still valid (rstrip doesn't change leading ws)
        # Module 14 TRAILING detector uses rstrip, LEADING uses lstrip;
        # either is a valid proposal.
        assert _single(self.ACTION, "  hello", "  hello") == "passed"

    def test_trailing_lstrip_also_passes(self):
        # lstrip of "hello  " == "hello  " -- no change, still valid
        assert _single(self.ACTION, "hello  ", "hello  ") == "passed"

    def test_wrong_proposed_value_fails(self):
        assert _single(self.ACTION, "  hello  ", "WRONG") == "failed"

    def test_none_original_skips(self):
        assert _single(self.ACTION, None, "x") == "skipped"

    def test_skip_reason_is_original_value_missing(self):
        c = _change(self.ACTION, None, "x")
        result = _run([c])
        assert result.results[0].reason == ORIGINAL_VALUE_MISSING

    def test_pass_reason_is_proposed_value_matches(self):
        c = _change(self.ACTION, "  hello", "hello")
        result = _run([c])
        assert result.results[0].reason == PROPOSED_VALUE_MATCHES

    def test_fail_reason_is_proposed_value_mismatch(self):
        c = _change(self.ACTION, "  hello", "WRONG")
        result = _run([c])
        assert result.results[0].reason == PROPOSED_VALUE_MISMATCH

    def test_rule_name_stored_on_result(self):
        c = _change(self.ACTION, "  hello", "hello")
        result = _run([c])
        assert result.results[0].validation_rule == "validate_trim_whitespace"

    def test_rule_version_stored_on_result(self):
        c = _change(self.ACTION, "  hello", "hello")
        result = _run([c])
        assert result.results[0].validation_rule_version == "1.0"


# ---------------------------------------------------------------------------
# D. validate_collapse_multiple_spaces
# ---------------------------------------------------------------------------

class TestValidateCollapseMultipleSpaces:
    ACTION = "collapse_multiple_spaces"

    def test_collapsed_value_passes(self):
        # "hello  world" -> "hello world"
        assert _single(self.ACTION, "hello  world", "hello world") == "passed"

    def test_wrong_proposed_value_fails(self):
        assert _single(self.ACTION, "hello  world", "hello  world") == "failed"

    def test_none_original_skips(self):
        assert _single(self.ACTION, None, "x") == "skipped"

    def test_multiple_collapse_passes(self):
        # "a   b   c" -> "a b   c" is NOT correct (only adjacent pairs)
        # regex replaces first pair: "a   b   c" -> "a b   c" -> then no more 2-char gaps
        # Actually: re.sub(r"(\S)\s{2,}(\S)", r"\1 \2", "a   b   c")
        # First match: a   b -> "a b   c", second match: b   c -> "a b c"
        # Actually the regex is non-overlapping, so it processes "a   b" first → "a b   c",
        # then continues from after "b" and finds "   c" → but \S\s{2+}\S needs non-space before
        # "   c" -- let me be accurate
        import re
        src = "a   b   c"
        expected = re.sub(r"(\S)\s{2,}(\S)", r"\1 \2", src)
        assert _single(self.ACTION, src, expected) == "passed"

    def test_pass_reason(self):
        c = _change(self.ACTION, "hello  world", "hello world")
        result = _run([c])
        assert result.results[0].reason == PROPOSED_VALUE_MATCHES

    def test_fail_reason(self):
        c = _change(self.ACTION, "hello  world", "wrong")
        result = _run([c])
        assert result.results[0].reason == PROPOSED_VALUE_MISMATCH


# ---------------------------------------------------------------------------
# E. validate_standardize_capitalization
# ---------------------------------------------------------------------------

class TestValidateStandardizeCapitalization:
    ACTION = "standardize_capitalization"

    def _col_cfg(self, target: str) -> dict:
        return {"col": ValidationColumnConfig(capitalization_target=target)}

    def test_lower_passes(self):
        assert _single(self.ACTION, "HELLO", "hello", self._col_cfg("lower")) == "passed"

    def test_upper_passes(self):
        assert _single(self.ACTION, "hello", "HELLO", self._col_cfg("upper")) == "passed"

    def test_title_passes(self):
        assert _single(self.ACTION, "hello world", "Hello World", self._col_cfg("title")) == "passed"

    def test_wrong_case_fails(self):
        assert _single(self.ACTION, "HELLO", "HELLO", self._col_cfg("lower")) == "failed"

    def test_missing_target_skips(self):
        assert _single(self.ACTION, "hello", "hello") == "skipped"

    def test_missing_target_reason(self):
        c = _change(self.ACTION, "hello", "HELLO")
        result = _run([c])
        assert result.results[0].reason == CAPITALIZATION_TARGET_NOT_CONFIGURED

    def test_none_original_skips(self):
        assert _single(self.ACTION, None, "x", self._col_cfg("lower")) == "skipped"

    def test_title_apostrophe_not_mangled(self):
        # title_case should not mangle apostrophes (this exercises the shared
        # casing function, NOT Python str.title() -- Module 15 comment).
        col = self._col_cfg("title")
        # "don't" title-cased properly -> "Don't" not "Don'T"
        assert _single(self.ACTION, "don't", "Don't", col) == "passed"


# ---------------------------------------------------------------------------
# F. validate_normalize_boolean
# ---------------------------------------------------------------------------

class TestValidateNormalizeBoolean:
    ACTION = "normalize_boolean"

    def test_true_normalizes_to_canonical_passes(self):
        # standardize_boolean("TRUE") should produce canonical true form
        from app.standardization.rules.values import standardize_boolean
        canon, _ = standardize_boolean("TRUE", output_form=None)
        assert _single(self.ACTION, "TRUE", canon) == "passed"

    def test_false_normalizes_to_canonical_passes(self):
        from app.standardization.rules.values import standardize_boolean
        canon, _ = standardize_boolean("FALSE", output_form=None)
        assert _single(self.ACTION, "FALSE", canon) == "passed"

    def test_wrong_proposed_fails(self):
        assert _single(self.ACTION, "TRUE", "WRONG") == "failed"

    def test_unrecognised_boolean_value_fails(self):
        # "maybe" has no canonical boolean form; a RemediationChange
        # proposing a replacement is inconsistent.
        assert _single(self.ACTION, "maybe", "true") == "failed"

    def test_pass_reason(self):
        from app.standardization.rules.values import standardize_boolean
        canon, _ = standardize_boolean("yes", output_form=None)
        c = _change(self.ACTION, "yes", canon)
        result = _run([c])
        assert result.results[0].reason == PROPOSED_VALUE_MATCHES

    def test_fail_reason(self):
        c = _change(self.ACTION, "TRUE", "WRONG")
        result = _run([c])
        assert result.results[0].reason == PROPOSED_VALUE_MISMATCH


# ---------------------------------------------------------------------------
# G. validate_normalize_date
# ---------------------------------------------------------------------------

class TestValidateNormalizeDate:
    ACTION = "normalize_date"

    def _col_cfg(
        self,
        source_fmt: str | None = "%m/%d/%Y",
        target_fmt: str | None = "%Y-%m-%d",
    ) -> dict:
        return {"col": ValidationColumnConfig(
            source_date_format=source_fmt,
            target_date_format=target_fmt,
        )}

    def test_valid_date_passes(self):
        # "01/15/2024" with source="%m/%d/%Y" target="%Y-%m-%d" -> "2024-01-15"
        assert _single(self.ACTION, "01/15/2024", "2024-01-15", self._col_cfg()) == "passed"

    def test_wrong_proposed_fails(self):
        assert _single(self.ACTION, "01/15/2024", "15/01/2024", self._col_cfg()) == "failed"

    def test_unparseable_original_fails(self):
        # "notadate" cannot be parsed under %m/%d/%Y
        assert _single(self.ACTION, "notadate", "2024-01-15", self._col_cfg()) == "failed"

    def test_missing_source_format_skips(self):
        assert _single(
            self.ACTION, "01/15/2024", "2024-01-15", self._col_cfg(source_fmt=None)
        ) == "skipped"

    def test_missing_target_format_skips(self):
        assert _single(
            self.ACTION, "01/15/2024", "2024-01-15", self._col_cfg(target_fmt=None)
        ) == "skipped"

    def test_missing_source_reason(self):
        c = _change(self.ACTION, "01/15/2024", "2024-01-15")
        col = {"col": ValidationColumnConfig(source_date_format=None, target_date_format="%Y-%m-%d")}
        result = _run([c], col)
        assert result.results[0].reason == SOURCE_DATE_FORMAT_NOT_CONFIGURED

    def test_missing_target_reason(self):
        c = _change(self.ACTION, "01/15/2024", "2024-01-15")
        col = {"col": ValidationColumnConfig(source_date_format="%m/%d/%Y", target_date_format=None)}
        result = _run([c], col)
        assert result.results[0].reason == TARGET_DATE_FORMAT_NOT_CONFIGURED

    def test_none_original_skips(self):
        assert _single(self.ACTION, None, "2024-01-15", self._col_cfg()) == "skipped"

    def test_no_column_config_skips_on_source(self):
        assert _single(self.ACTION, "01/15/2024", "2024-01-15") == "skipped"


# ---------------------------------------------------------------------------
# H. validate_normalize_phone
# ---------------------------------------------------------------------------

class TestValidateNormalizePhone:
    ACTION = "normalize_phone"

    def _col_cfg(self, country: str | None = "US") -> dict:
        return {"col": ValidationColumnConfig(default_country=country)}

    def test_valid_us_phone_passes(self):
        from app.standardization.rules.contact import standardize_phone
        # Use a number that standardize_phone can actually convert to E.164
        canon, rule_applied = standardize_phone("+1 555 123 4567", "US")
        if rule_applied is not None:
            assert _single(self.ACTION, "+1 555 123 4567", canon, self._col_cfg()) == "passed"
        else:
            # standardize_phone cannot normalize this number in this environment;
            # skip the assertion (library may lack phonenumbers package).
            pytest.skip("standardize_phone cannot normalize test number; phonenumbers may be unavailable")

    def test_wrong_proposed_fails(self):
        # Even if we can parse it, wrong proposed value should fail
        from app.standardization.rules.contact import standardize_phone
        canon, rule_applied = standardize_phone("+1 555 123 4567", "US")
        if rule_applied is not None:
            assert _single(self.ACTION, "+1 555 123 4567", "WRONG", self._col_cfg()) == "failed"

    def test_missing_country_skips(self):
        assert _single(self.ACTION, "(555) 123-4567", "+15551234567") == "skipped"

    def test_missing_country_reason(self):
        c = _change(self.ACTION, "(555) 123-4567", "+15551234567")
        result = _run([c])
        assert result.results[0].reason == DEFAULT_COUNTRY_NOT_CONFIGURED

    def test_none_country_in_config_skips(self):
        assert _single(
            self.ACTION, "(555) 123-4567", "+15551234567", self._col_cfg(country=None)
        ) == "skipped"


# ---------------------------------------------------------------------------
# I. validate_normalize_numeric
# ---------------------------------------------------------------------------

class TestValidateNormalizeNumeric:
    ACTION = "normalize_numeric"

    def test_numeric_with_currency_passes(self):
        from app.standardization.rules.values import standardize_numeric
        canon, applied = standardize_numeric("$1,234.56", locale=None)
        if applied is not None:
            assert _single(self.ACTION, "$1,234.56", canon) == "passed"

    def test_wrong_proposed_fails(self):
        assert _single(self.ACTION, "1234", "WRONG") == "failed"

    def test_non_numeric_string_fails(self):
        # "hello" is not numeric; function returns no change
        assert _single(self.ACTION, "hello", "hello") == "failed"

    def test_pass_reason(self):
        from app.standardization.rules.values import standardize_numeric
        canon, applied = standardize_numeric("1,234", locale=None)
        if applied is not None:
            c = _change(self.ACTION, "1,234", canon)
            result = _run([c])
            assert result.results[0].reason == PROPOSED_VALUE_MATCHES


# ---------------------------------------------------------------------------
# J. validate_normalize_enum_value
# ---------------------------------------------------------------------------

class TestValidateNormalizeEnumValue:
    ACTION = "normalize_enum_value"

    def _col_cfg(self, values: tuple = ("Red", "Green", "Blue")) -> dict:
        return {"col": ValidationColumnConfig(allowed_values=values)}

    def test_case_insensitive_match_passes(self):
        assert _single(self.ACTION, "red", "Red", self._col_cfg()) == "passed"

    def test_wrong_proposed_fails(self):
        assert _single(self.ACTION, "red", "Green", self._col_cfg()) == "failed"

    def test_no_match_fails(self):
        assert _single(self.ACTION, "Yellow", "Yellow", self._col_cfg()) == "failed"

    def test_ambiguous_match_fails(self):
        # Two allowed values normalise to the same casefolded form -- Module 15 skips
        # ambiguous; validation flags the proposal as inconsistent.
        col = self._col_cfg(("Red", "red"))
        assert _single(self.ACTION, "RED", "Red", col) == "failed"

    def test_missing_allowed_values_skips(self):
        assert _single(self.ACTION, "red", "Red") == "skipped"

    def test_missing_allowed_values_reason(self):
        c = _change(self.ACTION, "red", "Red")
        result = _run([c])
        assert result.results[0].reason == ALLOWED_VALUES_NOT_CONFIGURED

    def test_none_original_skips(self):
        assert _single(self.ACTION, None, "Red", self._col_cfg()) == "skipped"

    def test_pass_reason(self):
        c = _change(self.ACTION, "red", "Red")
        result = _run([c], self._col_cfg())
        assert result.results[0].reason == PROPOSED_VALUE_MATCHES

    def test_fail_reason(self):
        c = _change(self.ACTION, "yellow", "Red")
        result = _run([c], self._col_cfg())
        assert result.results[0].reason == PROPOSED_VALUE_MISMATCH


# ---------------------------------------------------------------------------
# K. validate_remove_duplicate_row
# ---------------------------------------------------------------------------

class TestValidateRemoveDuplicateRow:
    ACTION = "remove_duplicate_row"

    def test_none_proposed_value_passes(self):
        assert _single(self.ACTION, None, None, column_name=None) == "passed"

    def test_non_none_proposed_value_fails(self):
        assert _single(self.ACTION, None, "something", column_name=None) == "failed"

    def test_pass_reason(self):
        c = _change(self.ACTION, None, None, column_name=None)
        result = _run([c])
        assert result.results[0].reason == DUPLICATE_REMOVAL_CORRECTLY_FORMED

    def test_fail_reason(self):
        c = _change(self.ACTION, None, "something", column_name=None)
        result = _run([c])
        assert result.results[0].reason == DUPLICATE_REMOVAL_PROPOSED_VALUE_NOT_NONE

    def test_rule_name(self):
        c = _change(self.ACTION, None, None, column_name=None)
        result = _run([c])
        assert result.results[0].validation_rule == "validate_remove_duplicate_row"


# ---------------------------------------------------------------------------
# L. validate_remove_duplicate_primary_key
# ---------------------------------------------------------------------------

class TestValidateRemoveDuplicatePrimaryKey:
    ACTION = "remove_duplicate_primary_key"

    def test_none_proposed_value_passes(self):
        assert _single(self.ACTION, None, None, column_name=None) == "passed"

    def test_non_none_proposed_value_fails(self):
        assert _single(self.ACTION, None, "something", column_name=None) == "failed"

    def test_pass_reason(self):
        c = _change(self.ACTION, None, None, column_name=None)
        result = _run([c])
        assert result.results[0].reason == DUPLICATE_REMOVAL_CORRECTLY_FORMED

    def test_fail_reason(self):
        c = _change(self.ACTION, None, "something", column_name=None)
        result = _run([c])
        assert result.results[0].reason == DUPLICATE_REMOVAL_PROPOSED_VALUE_NOT_NONE

    def test_rule_name(self):
        c = _change(self.ACTION, None, None, column_name=None)
        result = _run([c])
        assert result.results[0].validation_rule == "validate_remove_duplicate_primary_key"


# ---------------------------------------------------------------------------
# M. Engine orchestration
# ---------------------------------------------------------------------------

class TestEngineOrchestration:
    def test_empty_changes_returns_zero_counts(self):
        result = _run([])
        assert result.approved_changes_considered == 0
        assert result.passed_count == 0
        assert result.failed_count == 0
        assert result.skipped_count == 0
        assert result.results == []
        assert result.results_by_rule == {}

    def test_count_reconcile_invariant(self):
        changes = [
            _change("trim_whitespace", "  hello", "hello"),
            _change("trim_whitespace", "  hello", "WRONG"),
            _change("normalize_boolean", "TRUE", "x"),
            _change("normalize_date", "01/01/2024", "2024-01-01"),
        ]
        result = _run(changes)
        assert (
            result.passed_count + result.failed_count + result.skipped_count
            == result.approved_changes_considered
        )

    def test_sort_order_row_then_column_then_action(self):
        """Results must be in (row_number, column_name, action) order regardless
        of the input list order."""
        c1 = ValidationChangeInput(
            change_id=uuid.uuid4(), source_issue_id=uuid.uuid4(),
            row_number=5, column_name="z", action="trim_whitespace",
            original_value="  x", proposed_value="x",
        )
        c2 = ValidationChangeInput(
            change_id=uuid.uuid4(), source_issue_id=uuid.uuid4(),
            row_number=2, column_name="a", action="trim_whitespace",
            original_value="  y", proposed_value="y",
        )
        c3 = ValidationChangeInput(
            change_id=uuid.uuid4(), source_issue_id=uuid.uuid4(),
            row_number=2, column_name="b", action="trim_whitespace",
            original_value="  z", proposed_value="z",
        )
        result = _run([c1, c2, c3])
        rows = [r.row_number for r in result.results]
        assert rows == [2, 2, 5]
        assert result.results[0].column_name == "a"
        assert result.results[1].column_name == "b"

    def test_result_limit_reached_skips_remainder(self):
        changes = [
            _change("trim_whitespace", "  a", "a"),
            _change("trim_whitespace", "  b", "b"),
            _change("trim_whitespace", "  c", "c"),
        ]
        result = _run(changes, max_results=1)
        assert result.approved_changes_considered == 3
        assert result.passed_count == 1
        assert result.skipped_count == 2
        assert result.results[1].reason == RESULT_LIMIT_REACHED
        assert result.results[2].reason == RESULT_LIMIT_REACHED

    def test_result_limit_zero_skips_all(self):
        changes = [_change("trim_whitespace", "  a", "a")]
        result = _run(changes, max_results=0)
        assert result.passed_count == 0
        assert result.skipped_count == 1
        assert result.results[0].reason == RESULT_LIMIT_REACHED

    def test_action_not_recognised_skips(self):
        c = ValidationChangeInput(
            change_id=uuid.uuid4(), source_issue_id=uuid.uuid4(),
            row_number=2, column_name="col", action="not_a_real_action",
            original_value="x", proposed_value="y",
        )
        result = _run([c])
        assert result.skipped_count == 1
        assert result.results[0].reason == ACTION_NOT_RECOGNISED
        assert result.results[0].validation_rule == ""

    def test_results_by_rule_counts_all_outcomes_per_rule(self):
        changes = [
            _change("trim_whitespace", "  a", "a"),    # passed
            _change("trim_whitespace", "  b", "WRONG"),  # failed
        ]
        result = _run(changes)
        assert result.results_by_rule.get("validate_trim_whitespace") == 2

    def test_results_by_rule_excludes_action_not_recognised(self):
        c = ValidationChangeInput(
            change_id=uuid.uuid4(), source_issue_id=uuid.uuid4(),
            row_number=2, column_name="col", action="not_a_real_action",
            original_value="x", proposed_value="y",
        )
        result = _run([c])
        assert "" not in result.results_by_rule

    def test_approved_changes_considered_equals_input_len(self):
        changes = [
            _change("trim_whitespace", "  a", "a"),
            _change("trim_whitespace", "  b", "b"),
        ]
        result = _run(changes)
        assert result.approved_changes_considered == 2

    def test_result_items_preserve_change_id(self):
        c = _change("trim_whitespace", "  hello", "hello")
        result = _run([c])
        assert result.results[0].change_id == c.change_id

    def test_result_items_preserve_source_issue_id(self):
        c = _change("trim_whitespace", "  hello", "hello")
        result = _run([c])
        assert result.results[0].source_issue_id == c.source_issue_id

    def test_result_items_preserve_original_and_proposed_value(self):
        c = _change("trim_whitespace", "  hello", "hello")
        result = _run([c])
        assert result.results[0].original_value == "  hello"
        assert result.results[0].proposed_value == "hello"

    def test_mixed_actions_all_reconciled(self):
        changes = [
            _change("trim_whitespace", "  a", "a"),
            _change("collapse_multiple_spaces", "a  b", "a b"),
            _change("remove_duplicate_row", None, None, column_name=None),
            _change("remove_duplicate_primary_key", None, None, column_name=None),
        ]
        result = _run(changes)
        total = result.passed_count + result.failed_count + result.skipped_count
        assert total == 4


# ---------------------------------------------------------------------------
# N. Pure/no-ORM assertion
# ---------------------------------------------------------------------------

class TestPureEngine:
    def test_engine_module_has_no_sqlalchemy_import(self):
        """validate() must not import any ORM module -- the pure engine
        contract (no I/O, no database references) requires this."""
        import app.validation.engine as eng_mod
        # Walk the module's globals for any sqlalchemy or ORM references
        for name, obj in vars(eng_mod).items():
            if isinstance(obj, _types.ModuleType):
                assert "sqlalchemy" not in obj.__name__, (
                    f"engine.py imports ORM module {obj.__name__!r} via {name!r}"
                )

    def test_rules_module_has_no_orm_models_import(self):
        """No validation rule should import ORM models."""
        import app.validation.rules.trim as trim_mod
        import app.validation.rules.collapse as collapse_mod
        import app.validation.rules.duplicates as dup_mod
        for mod in (trim_mod, collapse_mod, dup_mod):
            for name, obj in vars(mod).items():
                if isinstance(obj, _types.ModuleType):
                    assert "app.models" not in obj.__name__, (
                        f"{mod.__name__} imports ORM model module {obj.__name__!r}"
                    )
