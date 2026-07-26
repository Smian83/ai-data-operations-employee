"""Repeatability / determinism tests for the Module 18 Quality Control Engine.

Mirrors tests/test_validation_repeatability.py exactly in spirit:
  - Same QualityEngineInput → byte-identical QualityEngineResult every call.
  - Order of validation_results does not affect the result.
  - Different inputs → (usually) different results.
  - Multiple concurrent-style calls produce identical output.

No database, no ORM, no HTTP.
"""
from __future__ import annotations

import uuid
from copy import deepcopy

import pytest

from app.quality.engine import quality_control
from app.quality.types import (
    DataProfileSnapshot,
    EffectiveDatasetStats,
    IssueDetectionRunSnapshot,
    QualityEngineInput,
    QualityLimits,
    QualityThresholdConfig,
    ValidationResultSnapshot,
    ValidationRunSnapshot,
)


# ── Fixtures ──────────────────────────────────────────────────────────────────

def _val_result(outcome="passed", rule="validate_standardize_capitalization",
                col="city"):
    return ValidationResultSnapshot(
        id=uuid.uuid4(),
        outcome=outcome,
        rule_name=rule,
        column_name=col,
        source_issue_id=None,
        remediation_change_id=None,
    )


def _baseline_inputs() -> QualityEngineInput:
    """A rich set of inputs that exercises completeness, uniqueness, validity,
    consistency, unresolved_risk, and validation_coverage categories."""
    val_results = (
        _val_result("passed", "validate_standardize_capitalization", "city"),
        _val_result("passed", "validate_standardize_capitalization", "city"),
        _val_result("failed", "validate_normalize_boolean", "active"),
        _val_result("passed", "validate_normalize_boolean", "active"),
        _val_result("passed", "validate_trim_whitespace", "name"),
    )
    return QualityEngineInput(
        organization_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        data_source_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        validation_run=ValidationRunSnapshot(
            id=uuid.UUID("00000000-0000-0000-0000-000000000003"),
            approved_changes_considered=5,
            passed_count=4,
            failed_count=1,
            skipped_count=0,
            results_by_rule={
                "validate_standardize_capitalization": 2,
                "validate_normalize_boolean": 2,
                "validate_trim_whitespace": 1,
            },
        ),
        validation_results=val_results,
        issue_detection_run=IssueDetectionRunSnapshot(
            id=uuid.UUID("00000000-0000-0000-0000-000000000004"),
            total_issues_found=50,
        ),
        data_profile=DataProfileSnapshot(
            id=uuid.UUID("00000000-0000-0000-0000-000000000005"),
            row_count=1000,
            column_count=10,
            duplicate_row_count=20,
            missing_value_total=100,
            source_sha256="a" * 64,
        ),
        effective_stats=EffectiveDatasetStats(
            effective_row_count=980,
            effective_duplicate_row_count=10,
            effective_missing_value_total=50,
            unresolved_high_count=2,
            unresolved_critical_count=0,
            addressed_issue_count=30,
        ),
        issues=(),
        thresholds=QualityThresholdConfig(
            fail_score_threshold=60.0,
            pass_score_threshold=85.0,
            max_validation_failure_rate=0.05,
            max_validation_skip_rate=0.5,
            max_high_severity_unresolved=5,
            max_critical_severity_unresolved=0,
            max_warnings_for_clean_pass=3,
            category_weights=None,
            threshold_config_id=None,
            threshold_config_source="built_in_defaults",
        ),
        limits=QualityLimits(max_persisted_findings=10_000),
    )


# ── Tests ─────────────────────────────────────────────────────────────────────

class TestDeterminism:
    def test_identical_inputs_identical_output(self):
        inputs = _baseline_inputs()
        result1 = quality_control(inputs)
        result2 = quality_control(inputs)
        assert result1.overall_score == result2.overall_score
        assert result1.recommendation == result2.recommendation
        assert result1.total_findings == result2.total_findings
        assert result1.blocking_count == result2.blocking_count
        assert result1.warning_count == result2.warning_count
        assert result1.info_count == result2.info_count

    def test_identical_inputs_identical_findings_tuple(self):
        inputs = _baseline_inputs()
        result1 = quality_control(inputs)
        result2 = quality_control(inputs)
        assert len(result1.findings) == len(result2.findings)
        for f1, f2 in zip(result1.findings, result2.findings):
            assert f1.category == f2.category
            assert f1.rule_name == f2.rule_name
            assert f1.severity == f2.severity
            assert f1.reason == f2.reason
            assert f1.affected_row_count == f2.affected_row_count
            assert f1.affected_column == f2.affected_column

    def test_identical_inputs_identical_category_scores(self):
        inputs = _baseline_inputs()
        result1 = quality_control(inputs)
        result2 = quality_control(inputs)
        assert result1.category_scores == result2.category_scores

    def test_identical_inputs_identical_category_statuses(self):
        inputs = _baseline_inputs()
        result1 = quality_control(inputs)
        result2 = quality_control(inputs)
        assert result1.category_statuses == result2.category_statuses

    def test_identical_inputs_identical_weights_used(self):
        inputs = _baseline_inputs()
        result1 = quality_control(inputs)
        result2 = quality_control(inputs)
        assert result1.category_weights_used == result2.category_weights_used

    def test_many_calls_stable(self):
        inputs = _baseline_inputs()
        results = [quality_control(inputs) for _ in range(10)]
        first = results[0]
        for r in results[1:]:
            assert r.overall_score == first.overall_score
            assert r.recommendation == first.recommendation
            assert r.total_findings == first.total_findings


class TestInputOrderIndependence:
    """The engine must produce the same result regardless of the order that
    validation_results are passed in — deterministic sort happens internally."""

    def test_reversed_val_results_same_output(self):
        inputs = _baseline_inputs()
        # Reverse the validation_results tuple.
        reversed_inputs = QualityEngineInput(
            organization_id=inputs.organization_id,
            data_source_id=inputs.data_source_id,
            validation_run=inputs.validation_run,
            validation_results=tuple(reversed(inputs.validation_results)),
            issue_detection_run=inputs.issue_detection_run,
            data_profile=inputs.data_profile,
            effective_stats=inputs.effective_stats,
            issues=inputs.issues,
            thresholds=inputs.thresholds,
            limits=inputs.limits,
        )
        result1 = quality_control(inputs)
        result2 = quality_control(reversed_inputs)
        assert result1.overall_score == result2.overall_score
        assert result1.recommendation == result2.recommendation
        assert result1.total_findings == result2.total_findings
        assert result1.category_scores == result2.category_scores

    def test_shuffled_val_results_same_output(self):
        """Shuffling the tuple (using a fixed permutation) produces the same
        output — the consistency engine groups by (rule, column), so result
        order does not matter."""
        inputs = _baseline_inputs()
        # Fixed permutation: [2, 0, 4, 1, 3]
        orig = inputs.validation_results
        permuted = (orig[2], orig[0], orig[4], orig[1], orig[3])
        perm_inputs = QualityEngineInput(
            organization_id=inputs.organization_id,
            data_source_id=inputs.data_source_id,
            validation_run=inputs.validation_run,
            validation_results=permuted,
            issue_detection_run=inputs.issue_detection_run,
            data_profile=inputs.data_profile,
            effective_stats=inputs.effective_stats,
            issues=inputs.issues,
            thresholds=inputs.thresholds,
            limits=inputs.limits,
        )
        result1 = quality_control(inputs)
        result2 = quality_control(perm_inputs)
        assert result1.recommendation == result2.recommendation
        assert result1.total_findings == result2.total_findings
        assert result1.category_scores == result2.category_scores


class TestDifferentInputsDifferentResults:
    """Sanity check: meaningfully different inputs produce different results."""

    def test_high_missing_rate_vs_zero(self):
        base = _baseline_inputs()
        low_miss = QualityEngineInput(
            **{
                **base.__dict__,
                "effective_stats": EffectiveDatasetStats(
                    effective_row_count=1000,
                    effective_duplicate_row_count=0,
                    effective_missing_value_total=0,
                    unresolved_high_count=0,
                    unresolved_critical_count=0,
                    addressed_issue_count=0,
                ),
                "data_profile": DataProfileSnapshot(
                    id=base.data_profile.id,
                    row_count=1000,
                    column_count=10,
                    duplicate_row_count=0,
                    missing_value_total=0,
                    source_sha256="a" * 64,
                ),
            }
        )
        high_miss = QualityEngineInput(
            **{
                **base.__dict__,
                "effective_stats": EffectiveDatasetStats(
                    effective_row_count=1000,
                    effective_duplicate_row_count=0,
                    effective_missing_value_total=5000,
                    unresolved_high_count=0,
                    unresolved_critical_count=0,
                    addressed_issue_count=0,
                ),
                "data_profile": DataProfileSnapshot(
                    id=base.data_profile.id,
                    row_count=1000,
                    column_count=10,
                    duplicate_row_count=0,
                    missing_value_total=5000,
                    source_sha256="a" * 64,
                ),
            }
        )
        result_low = quality_control(low_miss)
        result_high = quality_control(high_miss)
        assert result_low.overall_score != result_high.overall_score

    def test_pass_vs_fail_inputs(self):
        base = _baseline_inputs()
        # Good: no missing, no duplicates, no unresolved.
        good = QualityEngineInput(
            organization_id=base.organization_id,
            data_source_id=base.data_source_id,
            validation_run=ValidationRunSnapshot(
                id=base.validation_run.id,
                approved_changes_considered=0,
                passed_count=0,
                failed_count=0,
                skipped_count=0,
                results_by_rule={},
            ),
            validation_results=(),
            issue_detection_run=IssueDetectionRunSnapshot(
                id=base.issue_detection_run.id,
                total_issues_found=0,
            ),
            data_profile=DataProfileSnapshot(
                id=base.data_profile.id,
                row_count=1000,
                column_count=5,
                duplicate_row_count=0,
                missing_value_total=0,
                source_sha256="a" * 64,
            ),
            effective_stats=EffectiveDatasetStats(
                effective_row_count=1000,
                effective_duplicate_row_count=0,
                effective_missing_value_total=0,
                unresolved_high_count=0,
                unresolved_critical_count=0,
                addressed_issue_count=0,
            ),
            issues=(),
            thresholds=QualityThresholdConfig(),
            limits=QualityLimits(max_persisted_findings=10_000),
        )
        # Bad: zero rows.
        bad = QualityEngineInput(
            **{**good.__dict__, "effective_stats": EffectiveDatasetStats(
                effective_row_count=0,
                effective_duplicate_row_count=0,
                effective_missing_value_total=0,
                unresolved_high_count=0,
                unresolved_critical_count=0,
                addressed_issue_count=0,
            )}
        )
        assert quality_control(good).recommendation == "PASS"
        assert quality_control(bad).recommendation == "FAIL"


class TestCategoryStatusDeterminism:
    def test_category_passed_status_deterministic(self):
        inputs = QualityEngineInput(
            organization_id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
            data_source_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
            validation_run=ValidationRunSnapshot(
                id=uuid.UUID("00000000-0000-0000-0000-000000000003"),
                approved_changes_considered=0,
                passed_count=0,
                failed_count=0,
                skipped_count=0,
                results_by_rule={},
            ),
            validation_results=(),
            issue_detection_run=IssueDetectionRunSnapshot(
                id=uuid.UUID("00000000-0000-0000-0000-000000000004"),
                total_issues_found=0,
            ),
            data_profile=DataProfileSnapshot(
                id=uuid.UUID("00000000-0000-0000-0000-000000000005"),
                row_count=1000,
                column_count=10,
                duplicate_row_count=0,
                missing_value_total=0,
                source_sha256="b" * 64,
            ),
            effective_stats=EffectiveDatasetStats(
                effective_row_count=1000,
                effective_duplicate_row_count=0,
                effective_missing_value_total=0,
                unresolved_high_count=0,
                unresolved_critical_count=0,
                addressed_issue_count=0,
            ),
            issues=(),
            thresholds=QualityThresholdConfig(),
            limits=QualityLimits(max_persisted_findings=10_000),
        )
        for _ in range(5):
            result = quality_control(inputs)
            assert result.category_statuses["completeness"] == "passed"
            assert result.category_statuses["uniqueness"] == "passed"
            assert result.category_statuses["validity"] == "skipped"
            assert result.category_statuses["consistency"] == "skipped"
            assert result.category_statuses["referential_integrity"] == "skipped"
            assert result.category_statuses["business_rule_compliance"] == "skipped"
            assert result.category_statuses["unresolved_risk"] == "skipped"
            assert result.category_statuses["validation_coverage"] == "skipped"
