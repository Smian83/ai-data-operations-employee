"""Module 15 Phase 2 engine integration tests: dispatch through the
registry, aggregation (changes_by_action / issues_skipped_count /
issues_considered_count), the two structural skip paths
(issue_type_not_remediable / row_not_found_in_dataset), multiple issues
targeting the same cell, duplicate-row/duplicate-primary-key proposals,
and the persisted-change-limit overflow-to-skipped behavior. Ordering and
pure repeatability (identical input -> identical output across many
calls) are covered separately in test_remediation_repeatability.py."""
import uuid

from app.detection.issue_types import (
    BOOLEAN_INCONSISTENCY,
    DUPLICATE_PRIMARY_KEY,
    DUPLICATE_ROW,
    INCONSISTENT_CAPITALIZATION,
    LEADING_WHITESPACE,
    MISSING_VALUE,
    TRAILING_WHITESPACE,
)
from app.remediation.engine import remediate
from app.remediation.skip_reasons import (
    DUPLICATE_REMOVAL_NOT_ENABLED,
    ISSUE_TYPE_NOT_REMEDIABLE,
    PERSISTED_CHANGE_LIMIT_REACHED,
    ROW_NOT_FOUND_IN_DATASET,
)
from app.remediation.types import (
    RemediationColumnConfig,
    RemediationDataset,
    RemediationDatasetConfigInput,
    RemediationIssueInput,
    RemediationLimits,
)


def _issue(issue_type, row_number=2, column_name="name", original_value="x", suggested_fix=None):
    return RemediationIssueInput(
        issue_id=uuid.uuid4(),
        row_number=row_number,
        column_name=column_name,
        issue_type=issue_type,
        original_value=original_value,
        suggested_fix=suggested_fix,
    )


def _dataset(row_count: int = 5) -> RemediationDataset:
    return RemediationDataset(headers=["name", "email"], rows=[["a", "b"]] * row_count)


def _limits(max_persisted_changes: int = 10_000) -> RemediationLimits:
    return RemediationLimits(max_persisted_changes=max_persisted_changes)


# --- structural skips --------------------------------------------------------


def test_issue_type_with_no_action_mapping_is_skipped_not_errored():
    issue = _issue(MISSING_VALUE, original_value=None)
    result = remediate(_dataset(), [issue], {}, RemediationDatasetConfigInput(), _limits())

    assert result.issues_considered_count == 1
    assert result.total_changes_count == 0
    assert result.issues_skipped_count == 1
    assert result.skipped[0].reason == ISSUE_TYPE_NOT_REMEDIABLE


def test_issue_whose_row_number_is_outside_the_dataset_is_skipped():
    issue = _issue(LEADING_WHITESPACE, row_number=999, original_value="  x", suggested_fix="x")
    result = remediate(_dataset(row_count=3), [issue], {}, RemediationDatasetConfigInput(), _limits())

    assert result.total_changes_count == 0
    assert result.issues_skipped_count == 1
    assert result.skipped[0].reason == ROW_NOT_FOUND_IN_DATASET


def test_row_number_at_exact_dataset_boundaries_is_accepted():
    # rows=[row0, row1, row2] -> valid CSV row_numbers are 2 (header+1) and
    # 4 (header + 3 rows); both boundaries must be treated as in-range.
    first = _issue(LEADING_WHITESPACE, row_number=2, original_value="  x", suggested_fix="x")
    last = _issue(LEADING_WHITESPACE, row_number=4, original_value="  y", suggested_fix="y")
    result = remediate(
        _dataset(row_count=3), [first, last], {}, RemediationDatasetConfigInput(), _limits()
    )

    assert result.total_changes_count == 2
    assert result.issues_skipped_count == 0


# --- aggregation --------------------------------------------------------------


def test_changes_by_action_and_counts_reconcile():
    issues = [
        _issue(LEADING_WHITESPACE, original_value="  a", suggested_fix="a"),
        _issue(TRAILING_WHITESPACE, original_value="b  ", suggested_fix="b"),
        _issue(MISSING_VALUE, original_value=None),
    ]
    result = remediate(_dataset(), issues, {}, RemediationDatasetConfigInput(), _limits())

    assert result.issues_considered_count == 3
    assert result.total_changes_count == 2
    assert result.issues_skipped_count == 1
    assert result.changes_by_action == {"trim_whitespace": 2}
    assert result.total_changes_count + result.issues_skipped_count == result.issues_considered_count


def test_zero_issues_produces_a_zeroed_result_not_an_error():
    result = remediate(_dataset(), [], {}, RemediationDatasetConfigInput(), _limits())

    assert result.issues_considered_count == 0
    assert result.total_changes_count == 0
    assert result.issues_skipped_count == 0
    assert result.changes_by_action == {}


# --- multiple issues targeting the same cell -----------------------------------


def test_two_different_issue_types_on_the_same_cell_are_both_proposed_independently():
    # Same row_number + column_name, two distinct issue_types (and
    # therefore two distinct source Issue rows) -- both must produce their
    # own independent RemediationChange; neither is dropped or merged.
    leading = _issue(LEADING_WHITESPACE, row_number=2, column_name="name", original_value="  bob", suggested_fix="bob")
    capitalization = _issue(
        INCONSISTENT_CAPITALIZATION, row_number=2, column_name="name", original_value="bob"
    )
    column_config = {"name": RemediationColumnConfig(capitalization_target="title")}

    result = remediate(_dataset(), [leading, capitalization], column_config, RemediationDatasetConfigInput(), _limits())

    assert result.total_changes_count == 2
    actions = {c.action for c in result.changes}
    assert actions == {"trim_whitespace", "standardize_capitalization"}
    assert all(c.row_number == 2 and c.column_name == "name" for c in result.changes)


# --- duplicate-row / duplicate-primary-key proposals ---------------------------


def test_duplicate_row_proposal_only_when_enabled():
    issue = _issue(DUPLICATE_ROW, row_number=3, column_name=None, original_value=None)

    disabled = remediate(_dataset(), [issue], {}, RemediationDatasetConfigInput(), _limits())
    assert disabled.total_changes_count == 0
    assert disabled.skipped[0].reason == DUPLICATE_REMOVAL_NOT_ENABLED

    enabled = remediate(
        _dataset(),
        [issue],
        {},
        RemediationDatasetConfigInput(remove_duplicate_rows_enabled=True),
        _limits(),
    )
    assert enabled.total_changes_count == 1
    change = enabled.changes[0]
    assert change.action == "remove_duplicate_row"
    assert change.proposed_value is None
    assert change.column_name is None


def test_duplicate_primary_key_proposal_carries_composite_column_name_verbatim():
    issue = _issue(
        DUPLICATE_PRIMARY_KEY,
        row_number=5,
        column_name="id+email",
        original_value="1, a@example.com",
    )
    result = remediate(
        _dataset(),
        [issue],
        {},
        RemediationDatasetConfigInput(remove_duplicate_primary_keys_enabled=True),
        _limits(),
    )

    assert result.total_changes_count == 1
    change = result.changes[0]
    assert change.action == "remove_duplicate_primary_key"
    assert change.column_name == "id+email"
    assert change.proposed_value is None
    assert change.original_value == "1, a@example.com"


def test_duplicate_row_and_duplicate_primary_key_gates_are_independent_in_the_engine():
    row_issue = _issue(DUPLICATE_ROW, row_number=2, column_name=None, original_value=None)
    key_issue = _issue(DUPLICATE_PRIMARY_KEY, row_number=3, column_name="id", original_value="1")
    result = remediate(
        _dataset(),
        [row_issue, key_issue],
        {},
        RemediationDatasetConfigInput(remove_duplicate_rows_enabled=True),
        _limits(),
    )

    assert result.total_changes_count == 1
    assert result.changes[0].action == "remove_duplicate_row"
    assert result.issues_skipped_count == 1
    assert result.skipped[0].reason == DUPLICATE_REMOVAL_NOT_ENABLED


# --- persisted-change limit ------------------------------------------------------


def test_persisted_change_limit_reclassifies_overflow_as_skipped():
    issues = [
        _issue(LEADING_WHITESPACE, row_number=n, original_value="  x", suggested_fix="x")
        for n in range(2, 7)  # 5 issues, all independently proposable
    ]
    result = remediate(_dataset(row_count=10), issues, {}, RemediationDatasetConfigInput(), _limits(max_persisted_changes=2))

    assert result.issues_considered_count == 5
    assert result.total_changes_count == 2
    assert result.issues_skipped_count == 3
    assert result.total_changes_count + result.issues_skipped_count == result.issues_considered_count
    assert all(s.reason == PERSISTED_CHANGE_LIMIT_REACHED for s in result.skipped)
    assert result.changes_by_action == {"trim_whitespace": 2}


def test_persisted_change_limit_keeps_the_first_changes_by_deterministic_order():
    issues = [
        _issue(LEADING_WHITESPACE, row_number=n, original_value="  x", suggested_fix="x")
        for n in (5, 2, 4, 3)  # deliberately out of order
    ]
    result = remediate(_dataset(row_count=10), issues, {}, RemediationDatasetConfigInput(), _limits(max_persisted_changes=2))

    kept_rows = sorted(c.row_number for c in result.changes)
    assert kept_rows == [2, 3]  # the two lowest row_numbers by sort order
    skipped_rows = sorted(s.row_number for s in result.skipped)
    assert skipped_rows == [4, 5]


def test_persisted_change_limit_does_not_affect_runs_under_the_ceiling():
    issues = [
        _issue(LEADING_WHITESPACE, row_number=n, original_value="  x", suggested_fix="x")
        for n in range(2, 5)
    ]
    result = remediate(_dataset(row_count=10), issues, {}, RemediationDatasetConfigInput(), _limits(max_persisted_changes=1_000))

    assert result.total_changes_count == 3
    assert result.issues_skipped_count == 0


# --- source_issue_id / original_value passthrough --------------------------------


def test_source_issue_id_is_always_populated_and_mandatory():
    issue = _issue(LEADING_WHITESPACE, original_value="  x", suggested_fix="x")
    result = remediate(_dataset(), [issue], {}, RemediationDatasetConfigInput(), _limits())

    assert result.changes[0].source_issue_id == issue.issue_id


def test_confidence_is_always_exactly_one():
    issue = _issue(LEADING_WHITESPACE, original_value="  x", suggested_fix="x")
    result = remediate(_dataset(), [issue], {}, RemediationDatasetConfigInput(), _limits())

    assert result.changes[0].confidence == 1.0
