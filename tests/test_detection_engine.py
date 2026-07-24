"""Module 14 Phase 2 integration tests for app.detection.engine.
detect_issues(). Pure -- no DB, no client. Covers: multi-rule aggregation
across a realistic dataset, deterministic sort order, the persisted-issue
cap leaving summary totals uncapped (approved correction #7), zero-issue
input, and the read-only guarantee (the input DetectionDataset is never
mutated)."""
import copy

from app.detection.engine import detect_issues
from app.detection.types import ColumnRuleConfig, DetectionDataset, DetectionLimits


def _limits(max_persisted_issues: int = 10_000) -> DetectionLimits:
    return DetectionLimits(max_persisted_issues=max_persisted_issues)


def test_detect_issues_on_fully_clean_data_yields_nothing():
    dataset = DetectionDataset(
        headers=["id", "name"],
        rows=[["1", "Ada"], ["2", "Grace"]],
        structural_issues=[],
        column_rules={},
    )
    result = detect_issues(dataset, _limits())

    assert result.findings == []
    assert result.total_issues_found == 0
    assert result.persisted_issue_count == 0
    assert result.issues_by_severity == {}
    assert result.issues_by_type == {}
    assert result.rows_scanned == 2
    assert result.columns_scanned == 2


def test_detect_issues_aggregates_across_multiple_independent_rules():
    headers = ["id", "email", "age", "name"]
    rows = [
        ["1", "good@example.com", "30", "John Smith"],
        ["2", "bad-email", "31", " Jane Doe"],
        ["1", "dup@example.com", "9999", "john smith"],
        ["3", "", "thirty", "Bob  Jones"],
    ]
    column_rules = {
        "id": ColumnRuleConfig(is_primary_key=True),
        "email": ColumnRuleConfig(expected_type="email", is_required=True),
        "age": ColumnRuleConfig(
            expected_type="numeric", outlier_enabled=True, outlier_zscore_threshold=1.0
        ),
    }
    dataset = DetectionDataset(headers=headers, rows=rows, column_rules=column_rules)

    result = detect_issues(dataset, _limits())

    assert result.total_issues_found == 8
    assert result.persisted_issue_count == 8
    assert result.rows_scanned == 4
    assert result.columns_scanned == 4
    assert sum(result.issues_by_severity.values()) == result.total_issues_found
    assert sum(result.issues_by_type.values()) == result.total_issues_found
    # Deterministic sort: (row_number, column_name, issue_type) ascending.
    sort_keys = [(f.row_number, f.column_name or "", f.issue_type) for f in result.findings]
    assert sort_keys == sorted(sort_keys)


def test_detect_issues_caps_persisted_findings_but_keeps_true_totals():
    # Five rows, each with a leading-whitespace value -- five findings.
    headers = ["name"]
    rows = [[" a"], [" b"], [" c"], [" d"], [" e"]]
    dataset = DetectionDataset(headers=headers, rows=rows)

    result = detect_issues(dataset, _limits(max_persisted_issues=2))

    assert result.total_issues_found == 5
    assert result.persisted_issue_count == 2
    assert len(result.findings) == 2
    # Summary totals reflect ALL 5 findings, not just the 2 persisted ones.
    assert result.issues_by_type["leading_whitespace"] == 5
    assert result.issues_by_severity["INFO"] == 5


def test_detect_issues_does_not_mutate_the_input_dataset():
    headers = ["id", "name"]
    rows = [["1", " Ada "], ["1", "Ada"]]
    column_rules = {"id": ColumnRuleConfig(is_primary_key=True)}
    dataset = DetectionDataset(
        headers=headers, rows=rows, structural_issues=[], column_rules=column_rules
    )
    before = copy.deepcopy((dataset.headers, dataset.rows, dataset.structural_issues))

    detect_issues(dataset, _limits())

    after = (dataset.headers, dataset.rows, dataset.structural_issues)
    assert before == after
