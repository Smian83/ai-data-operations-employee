"""Module 14 Phase 2 repeatability tests. Referenced directly by
app.detection.base.DetectionRule's own docstring: "Calling detect() twice
with the same dataset must yield the same findings in the same order
every time." Verifies this holds for the full engine (every registered
rule together) and spot-checks a couple of rules whose internals use
dict/set/Counter iteration, where accidental non-determinism would most
likely sneak in."""
from app.detection.engine import detect_issues
from app.detection.rules.boolean import BooleanInconsistencyRule
from app.detection.rules.capitalization import InconsistentCapitalizationRule
from app.detection.types import ColumnRuleConfig, DetectionDataset, DetectionLimits


def _big_realistic_dataset() -> DetectionDataset:
    headers = ["id", "email", "phone", "age", "signup_date", "status", "name", "active"]
    rows = [
        ["1", "a@example.com", "+14155552671", "34", "2024-01-01", "active", "Ada Lovelace", "true"],
        ["2", "bad-email", "5551234567", "35", "01/02/2024", "bogus", " Grace Hopper", "1"],
        ["1", "a@example.com", "+14155552671", "9999", "2024-01-03", "active", "ADA LOVELACE", "maybe"],
        ["3", "", "", "thirty", "2024-01-04", "inactive", "Bob  Jones", "false"],
        ["4", "n/a", "+442071838750", "40", "2024-01-05", "active", "carol white", "no"],
    ]
    column_rules = {
        "id": ColumnRuleConfig(is_primary_key=True),
        "email": ColumnRuleConfig(expected_type="email", is_required=True),
        "phone": ColumnRuleConfig(expected_type="phone"),
        "age": ColumnRuleConfig(
            expected_type="numeric", outlier_enabled=True, outlier_zscore_threshold=1.5
        ),
        "status": ColumnRuleConfig(allowed_values=("active", "inactive")),
        "name": ColumnRuleConfig(capitalization_check_enabled=True),
        "active": ColumnRuleConfig(expected_type="boolean"),
    }
    return DetectionDataset(headers=headers, rows=rows, column_rules=column_rules)


def test_full_engine_is_repeatable_across_many_runs():
    dataset = _big_realistic_dataset()
    limits = DetectionLimits(max_persisted_issues=1_000)

    results = [detect_issues(dataset, limits) for _ in range(5)]

    first = results[0]
    for result in results[1:]:
        assert result.findings == first.findings
        assert result.total_issues_found == first.total_issues_found
        assert result.issues_by_severity == first.issues_by_severity
        assert result.issues_by_type == first.issues_by_type


def test_boolean_inconsistency_rule_is_repeatable_despite_counter_use():
    dataset = DetectionDataset(
        headers=["active"],
        rows=[["true"], ["false"], ["1"], ["0"], ["yes"], ["no"], ["y"], ["garbage"]],
        column_rules={"active": ColumnRuleConfig(expected_type="boolean")},
    )
    rule = BooleanInconsistencyRule()

    runs = [list(rule.detect(dataset)) for _ in range(5)]
    assert all(run == runs[0] for run in runs)


def test_capitalization_rule_is_repeatable_despite_counter_use():
    dataset = DetectionDataset(
        headers=["name"],
        rows=[["Ada Lovelace"], ["GRACE HOPPER"], ["bob jones"], ["Carol White"]],
        column_rules={"name": ColumnRuleConfig(capitalization_check_enabled=True)},
    )
    rule = InconsistentCapitalizationRule()

    runs = [list(rule.detect(dataset)) for _ in range(5)]
    assert all(run == runs[0] for run in runs)
