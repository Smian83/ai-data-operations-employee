"""Module 15 Phase 2 repeatability and ordering tests. Referenced directly
by app.remediation.base.RemediationRule's own docstring: "Calling
propose() twice ... must return an identical RuleOutcome every time", and
by app.remediation.engine.remediate's own docstring: "the same issue set +
column_config + dataset_config always produces byte-identical output ...
regardless of [input] order". Mirrors tests/test_detection_repeatability.py's
shape exactly."""
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
from app.remediation.engine import remediate
from app.remediation.types import (
    RemediationColumnConfig,
    RemediationDataset,
    RemediationDatasetConfigInput,
    RemediationIssueInput,
    RemediationLimits,
)


def _big_realistic_issue_set() -> list[RemediationIssueInput]:
    return [
        RemediationIssueInput(uuid.uuid4(), 2, "name", LEADING_WHITESPACE, "  Ada", "Ada"),
        RemediationIssueInput(uuid.uuid4(), 3, "name", TRAILING_WHITESPACE, "Grace  ", "Grace"),
        RemediationIssueInput(
            uuid.uuid4(), 4, "name", MULTIPLE_INTERNAL_SPACES, "Bob   Jones", "Bob Jones"
        ),
        RemediationIssueInput(uuid.uuid4(), 5, "name", INCONSISTENT_CAPITALIZATION, "carol white", None),
        RemediationIssueInput(uuid.uuid4(), 2, "active", BOOLEAN_INCONSISTENCY, "1", None),
        RemediationIssueInput(uuid.uuid4(), 6, "signup_date", INVALID_DATE, "01/15/2024", None),
        RemediationIssueInput(uuid.uuid4(), 7, "phone", INVALID_PHONE, "4155552671", None),
        RemediationIssueInput(uuid.uuid4(), 8, "age", INVALID_NUMERIC, "1,234", None),
        RemediationIssueInput(uuid.uuid4(), 9, "status", INVALID_ENUM_VALUE, "ACTIVE", None),
        RemediationIssueInput(uuid.uuid4(), 10, None, DUPLICATE_ROW, None, None),
        RemediationIssueInput(uuid.uuid4(), 11, "id", DUPLICATE_PRIMARY_KEY, "1", None),
    ]


def _dataset() -> RemediationDataset:
    return RemediationDataset(
        headers=["name", "active", "signup_date", "phone", "age", "status", "id"],
        rows=[[""] * 7 for _ in range(15)],
    )


def _column_config() -> dict[str, RemediationColumnConfig]:
    return {
        "name": RemediationColumnConfig(capitalization_target="title"),
        "signup_date": RemediationColumnConfig(
            source_date_format="%m/%d/%Y", target_date_format="%Y-%m-%d"
        ),
        "phone": RemediationColumnConfig(default_country="US"),
        "status": RemediationColumnConfig(allowed_values=("active", "inactive")),
    }


def _dataset_config() -> RemediationDatasetConfigInput:
    return RemediationDatasetConfigInput(
        remove_duplicate_rows_enabled=True, remove_duplicate_primary_keys_enabled=True
    )


def _limits() -> RemediationLimits:
    return RemediationLimits(max_persisted_changes=1_000)


def test_full_engine_is_repeatable_across_many_runs():
    issues = _big_realistic_issue_set()
    dataset, column_config, dataset_config, limits = (
        _dataset(),
        _column_config(),
        _dataset_config(),
        _limits(),
    )

    results = [remediate(dataset, issues, column_config, dataset_config, limits) for _ in range(5)]

    first = results[0]
    for result in results[1:]:
        assert result.changes == first.changes
        assert result.skipped == first.skipped
        assert result.issues_considered_count == first.issues_considered_count
        assert result.total_changes_count == first.total_changes_count
        assert result.issues_skipped_count == first.issues_skipped_count
        assert result.changes_by_action == first.changes_by_action


def test_output_is_identical_regardless_of_input_issue_order():
    issues = _big_realistic_issue_set()
    dataset, column_config, dataset_config, limits = (
        _dataset(),
        _column_config(),
        _dataset_config(),
        _limits(),
    )

    forward = remediate(dataset, issues, column_config, dataset_config, limits)
    reversed_input = remediate(dataset, list(reversed(issues)), column_config, dataset_config, limits)

    # Reordering the input must not reorder or change the output at all --
    # same identity-independent-of-arrival-order guarantee
    # app.detection.engine.detect_issues already provides.
    assert forward.changes == reversed_input.changes
    assert forward.skipped == reversed_input.skipped


def test_changes_are_sorted_by_row_number_then_column_name_then_issue_type():
    # Deliberately out-of-order input, including a tie on row_number
    # broken only by column_name, and (via the two whitespace issue
    # types) a tie on (row_number, column_name) broken by issue_type.
    issues = [
        RemediationIssueInput(uuid.uuid4(), 5, "z_col", LEADING_WHITESPACE, "  b", "b"),
        RemediationIssueInput(uuid.uuid4(), 2, "a_col", TRAILING_WHITESPACE, "a  ", "a"),
        RemediationIssueInput(uuid.uuid4(), 2, "a_col", LEADING_WHITESPACE, "  a", "a"),
        RemediationIssueInput(uuid.uuid4(), 2, "b_col", LEADING_WHITESPACE, "  c", "c"),
    ]
    dataset = RemediationDataset(headers=["a_col", "b_col", "z_col"], rows=[[""] * 3 for _ in range(10)])

    result = remediate(dataset, issues, {}, RemediationDatasetConfigInput(), _limits())

    ordering = [(c.row_number, c.column_name, c.action) for c in result.changes]
    assert ordering == [
        (2, "a_col", "trim_whitespace"),  # leading_whitespace sorts before trailing_whitespace
        (2, "a_col", "trim_whitespace"),
        (2, "b_col", "trim_whitespace"),
        (5, "z_col", "trim_whitespace"),
    ]
    # The two row=2/a_col entries: LEADING_WHITESPACE issue_type ("leading_whitespace")
    # sorts before TRAILING_WHITESPACE ("trailing_whitespace") alphabetically.
    assert result.changes[0].proposed_value == "a"  # from the LEADING_WHITESPACE issue
    assert result.changes[1].proposed_value == "a"  # from the TRAILING_WHITESPACE issue


def test_skipped_list_is_sorted_by_the_same_key_as_changes():
    from app.detection.issue_types import MISSING_VALUE

    issues = [
        RemediationIssueInput(uuid.uuid4(), 9, "col", MISSING_VALUE, None, None),
        RemediationIssueInput(uuid.uuid4(), 2, "col", MISSING_VALUE, None, None),
        RemediationIssueInput(uuid.uuid4(), 5, "col", MISSING_VALUE, None, None),
    ]
    dataset = RemediationDataset(headers=["col"], rows=[[""] for _ in range(10)])

    result = remediate(dataset, issues, {}, RemediationDatasetConfigInput(), _limits())

    assert [s.row_number for s in result.skipped] == [2, 5, 9]


def test_boolean_action_is_repeatable_despite_no_internal_ordering_state():
    issue = RemediationIssueInput(uuid.uuid4(), 2, "active", BOOLEAN_INCONSISTENCY, "yes", None)
    dataset = RemediationDataset(headers=["active"], rows=[[""] for _ in range(5)])

    runs = [
        remediate(dataset, [issue], {}, RemediationDatasetConfigInput(), _limits()) for _ in range(5)
    ]
    assert all(run.changes == runs[0].changes for run in runs)
