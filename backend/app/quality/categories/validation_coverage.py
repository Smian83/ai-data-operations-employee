"""Validation Coverage category — measures how many approved RemediationChange
results were actually evaluated (not skipped) during the validation run.

A high skip rate indicates that a large fraction of changes could not be
validated — e.g. because the validation rule's preconditions were not met.
This degrades confidence in the overall quality assessment.

Applicable when: ValidationRun.approved_changes_considered > 0.
Same applicability condition as validity — if no changes were considered,
there is no coverage signal (skipped).

Score formula:
    skip_rate = skipped_count / approved_changes_considered
    score = max(0.0, 100.0 * (1.0 - skip_rate))

Finding severity:
    skip_rate > max_validation_skip_rate → BLOCKING (HIGH_SKIP_RATE)
        Forces release_recommendation = FAIL via blocking_count > 0
        (Section 10, condition 2). This is the correct mechanism per
        Section 11: "FAIL if skipped_count/total > max_validation_skip_rate
        AND that causes blocking".
    skip_rate > 0 (but ≤ threshold) → WARNING (RESULTS_SKIPPED)
    skip_rate == 0 → no finding (perfect coverage)

Default weight: 15 (Section 6.1).
"""
from __future__ import annotations

from app.quality.reasons import HIGH_SKIP_RATE, RESULTS_SKIPPED
from app.quality.types import (
    CategoryEvaluationResult,
    QualityEngineInput,
    QualityFindingResult,
)

_CATEGORY = "validation_coverage"
_RULE_NAME = "validation_coverage_skip_rate"
_RULE_VERSION = "1.0"


class ValidationCoverageCategory:
    category_name: str = _CATEGORY
    default_weight: int = 15
    category_rule_version: str = _RULE_VERSION

    def is_applicable(self, inputs: QualityEngineInput) -> bool:
        return inputs.validation_run.approved_changes_considered > 0

    def evaluate(self, inputs: QualityEngineInput) -> CategoryEvaluationResult:
        val_run = inputs.validation_run
        total = val_run.approved_changes_considered
        skipped = val_run.skipped_count

        skip_rate = skipped / total
        score = max(0.0, min(100.0, 100.0 * (1.0 - skip_rate)))

        if skipped == 0:
            return CategoryEvaluationResult(score=score, findings=())

        max_skip_rate = inputs.thresholds.max_validation_skip_rate

        if skip_rate > max_skip_rate:
            severity = "blocking"
            reason = HIGH_SKIP_RATE
        else:
            severity = "warning"
            reason = RESULTS_SKIPPED

        finding = QualityFindingResult(
            category=_CATEGORY,
            rule_name=_RULE_NAME,
            rule_version=_RULE_VERSION,
            severity=severity,
            outcome="failed",
            reason=reason,
            affected_row_count=skipped,
        )
        return CategoryEvaluationResult(score=score, findings=(finding,))
