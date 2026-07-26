"""Repeatability tests for the Module 17 Validation Engine.

Mirrors tests/test_remediation_repeatability.py and
tests/test_detection_repeatability.py: calling validate() twice with the
same inputs must always return byte-identical results. No ORM, no DB --
pure engine tests only.

Three repeatability properties verified:
  1. Identical output on identical input (determinism).
  2. Stable ordering -- permuting the input list does not change the
     output result list (since the engine sorts changes internally).
  3. Per-rule version stability -- rule_version stored on every result
     is identical across repeated calls.
"""
from __future__ import annotations

import copy
import random
import uuid

import pytest

from app.validation.engine import validate
from app.validation.types import (
    ValidationChangeInput,
    ValidationColumnConfig,
    ValidationLimits,
)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def _limits(n: int = 10_000) -> ValidationLimits:
    return ValidationLimits(max_persisted_results=n)


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


def _mixed_change_set() -> tuple[list[ValidationChangeInput], dict[str, ValidationColumnConfig]]:
    """A representative mixed set of changes exercising all 10 rules."""
    changes = [
        _change("trim_whitespace", "  hello", "hello", row_number=2),
        _change("trim_whitespace", "world  ", "world", row_number=3),
        _change("collapse_multiple_spaces", "a  b", "a b", row_number=4),
        _change("standardize_capitalization", "HELLO", "hello", column_name="cap_col", row_number=5),
        _change("normalize_boolean", "TRUE", None, row_number=6),  # proposed filled below
        _change("normalize_date", "01/15/2024", "2024-01-15", column_name="date_col", row_number=7),
        _change("normalize_phone", "(555) 123-4567", None, column_name="phone_col", row_number=8),
        _change("normalize_numeric", "1,234", None, row_number=9),
        _change("normalize_enum_value", "red", "Red", column_name="enum_col", row_number=10),
        _change("remove_duplicate_row", None, None, column_name=None, row_number=11),
        _change("remove_duplicate_primary_key", None, None, column_name=None, row_number=12),
    ]

    # Fill in correct proposed values for boolean, phone, numeric
    from app.standardization.rules.values import standardize_boolean, standardize_numeric
    from app.standardization.rules.contact import standardize_phone

    bool_canon, _ = standardize_boolean("TRUE", output_form=None)
    num_canon, _ = standardize_numeric("1,234", locale=None)
    phone_canon, _ = standardize_phone("(555) 123-4567", "US")

    # Replace the placeholder changes
    changes[4] = _change("normalize_boolean", "TRUE", bool_canon, row_number=6)
    if phone_canon is not None:
        changes[6] = _change("normalize_phone", "(555) 123-4567", phone_canon,
                             column_name="phone_col", row_number=8)
    if num_canon is not None:
        changes[7] = _change("normalize_numeric", "1,234", num_canon, row_number=9)

    column_config: dict[str, ValidationColumnConfig] = {
        "cap_col": ValidationColumnConfig(capitalization_target="lower"),
        "date_col": ValidationColumnConfig(
            source_date_format="%m/%d/%Y",
            target_date_format="%Y-%m-%d",
        ),
        "phone_col": ValidationColumnConfig(default_country="US"),
        "enum_col": ValidationColumnConfig(allowed_values=("Red", "Green", "Blue")),
    }
    return changes, column_config


# ---------------------------------------------------------------------------
# 1. Determinism: identical output on identical input
# ---------------------------------------------------------------------------

class TestValidationDeterminism:
    def test_same_input_produces_identical_outcome_list(self):
        changes, col_cfg = _mixed_change_set()
        r1 = validate(changes, col_cfg, _limits())
        r2 = validate(changes, col_cfg, _limits())
        assert len(r1.results) == len(r2.results)
        for item1, item2 in zip(r1.results, r2.results):
            assert item1.outcome == item2.outcome
            assert item1.reason == item2.reason
            assert item1.validation_rule == item2.validation_rule
            assert item1.validation_rule_version == item2.validation_rule_version

    def test_same_input_produces_identical_counts(self):
        changes, col_cfg = _mixed_change_set()
        r1 = validate(changes, col_cfg, _limits())
        r2 = validate(changes, col_cfg, _limits())
        assert r1.passed_count == r2.passed_count
        assert r1.failed_count == r2.failed_count
        assert r1.skipped_count == r2.skipped_count
        assert r1.approved_changes_considered == r2.approved_changes_considered

    def test_same_input_produces_identical_results_by_rule(self):
        changes, col_cfg = _mixed_change_set()
        r1 = validate(changes, col_cfg, _limits())
        r2 = validate(changes, col_cfg, _limits())
        assert r1.results_by_rule == r2.results_by_rule

    def test_single_rule_determinism_across_all_actions(self):
        """Each action's rule is deterministic: 5 calls with same input → same outcome."""
        from app.validation.registry import VALIDATION_RULES
        for rule in VALIDATION_RULES:
            # Build a minimal change that should produce skipped or passed
            if rule.action in ("remove_duplicate_row", "remove_duplicate_primary_key"):
                c = _change(rule.action, None, None, column_name=None)
                col_cfg = {}
            else:
                c = _change(rule.action, "test_value", "test_value")
                col_cfg = {}
            outcomes = set()
            for _ in range(5):
                r = validate([c], col_cfg, _limits())
                outcomes.add(r.results[0].outcome)
            assert len(outcomes) == 1, (
                f"rule {rule.rule_name!r} produced inconsistent outcomes: {outcomes}"
            )


# ---------------------------------------------------------------------------
# 2. Input-order independence
# ---------------------------------------------------------------------------

class TestInputOrderIndependence:
    def test_shuffled_input_produces_same_sorted_results(self):
        """validate() sorts changes internally; shuffling the input must
        not change the result list (given same change_ids in same order
        of sort key)."""
        changes, col_cfg = _mixed_change_set()
        r_original = validate(changes, col_cfg, _limits())

        shuffled = changes.copy()
        random.shuffle(shuffled)
        r_shuffled = validate(shuffled, col_cfg, _limits())

        # Result lists should be in the same sorted order regardless of input order
        assert len(r_original.results) == len(r_shuffled.results)
        for item_orig, item_shuf in zip(r_original.results, r_shuffled.results):
            assert item_orig.row_number == item_shuf.row_number
            assert item_orig.column_name == item_shuf.column_name
            assert item_orig.action_matches_rule(item_shuf) if hasattr(item_orig, 'action_matches_rule') else (
                item_orig.validation_rule == item_shuf.validation_rule
            )
            assert item_orig.outcome == item_shuf.outcome

    def test_reversed_input_same_counts(self):
        changes, col_cfg = _mixed_change_set()
        r_fwd = validate(changes, col_cfg, _limits())
        r_rev = validate(list(reversed(changes)), col_cfg, _limits())
        assert r_fwd.passed_count == r_rev.passed_count
        assert r_fwd.failed_count == r_rev.failed_count
        assert r_fwd.skipped_count == r_rev.skipped_count


# ---------------------------------------------------------------------------
# 3. Per-rule version stability
# ---------------------------------------------------------------------------

class TestRuleVersionStability:
    def test_rule_version_same_across_calls(self):
        """Calling validate() twice must produce identical rule_version
        strings on each result item -- the version is a module constant,
        never computed from input data."""
        changes, col_cfg = _mixed_change_set()
        r1 = validate(changes, col_cfg, _limits())
        r2 = validate(changes, col_cfg, _limits())
        for item1, item2 in zip(r1.results, r2.results):
            assert item1.validation_rule_version == item2.validation_rule_version

    def test_rule_version_non_empty_on_all_results(self):
        changes, col_cfg = _mixed_change_set()
        result = validate(changes, col_cfg, _limits())
        for item in result.results:
            if item.validation_rule:  # skip ACTION_NOT_RECOGNISED edge case
                assert item.validation_rule_version, (
                    f"empty rule_version on result for rule {item.validation_rule!r}"
                )


# ---------------------------------------------------------------------------
# 4. Count-reconcile invariant across all inputs
# ---------------------------------------------------------------------------

class TestCountReconcileInvariant:
    def test_invariant_holds_for_mixed_set(self):
        changes, col_cfg = _mixed_change_set()
        r = validate(changes, col_cfg, _limits())
        assert r.passed_count + r.failed_count + r.skipped_count == r.approved_changes_considered

    def test_invariant_holds_for_empty_input(self):
        r = validate([], {}, _limits())
        assert r.passed_count + r.failed_count + r.skipped_count == 0

    def test_invariant_holds_when_limit_applied(self):
        changes, col_cfg = _mixed_change_set()
        r = validate(changes, col_cfg, _limits(n=3))
        assert r.passed_count + r.failed_count + r.skipped_count == r.approved_changes_considered

    def test_invariant_holds_for_all_failed(self):
        changes = [
            _change("trim_whitespace", "  hello", "WRONG"),
            _change("trim_whitespace", "world  ", "WRONG"),
        ]
        r = validate(changes, {}, _limits())
        assert r.passed_count + r.failed_count + r.skipped_count == 2
        assert r.failed_count == 2

    def test_invariant_holds_for_all_skipped_missing_config(self):
        changes = [
            _change("normalize_date", "01/01/2024", "2024-01-01"),  # no config
            _change("normalize_phone", "(555) 123-4567", "+15551234567"),  # no config
        ]
        r = validate(changes, {}, _limits())
        assert r.passed_count + r.failed_count + r.skipped_count == 2
        assert r.skipped_count == 2
