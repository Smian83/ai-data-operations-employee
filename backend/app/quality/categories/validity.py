"""Validity category — measures the proportion of approved RemediationChanges
that passed validation.

Score formula:
    pass_rate = passed_count / approved_changes_considered
    score = 100.0 * pass_rate

Applicable when: ValidationRun.approved_changes_considered > 0.
If zero changes were considered, the category is not applicable (skipped).
Architecture Section 11: "validity = 100 vacuously" for zero-change runs —
this is expressed as the category being non-applicable (skipped), which is
the correct interpretation: no changes → no validity signal.

The global FAIL condition for validation failure rate (Section 10, condition 4:
failed_count / approved_changes_considered > max_validation_failure_rate) is
evaluated at the release-decision level in the engine, not here. This category
only produces the category score and findings based on the raw pass rate.

Default weight: 25 (Section 6.1).
"""
from __future__ import annotations

from app.quality.reasons import (
    CRITICAL_VALIDATION_FAILURE_RATE,
    HIGH_VALIDATION_FAILURE_RATE,
    VALIDATION_FAILURES_PRESENT,
)
from app.quality.types import (
    CategoryEvaluationResult,
    QualityEngineInput,
    QualityFindingResult,
)

_CATEGORY = "validity"
_RULE_NAME = "validity_validation_pass_rate"
_RULE_VERSION = "1.0"

_FAIL_THRESHOLD = 60.0
_WARN_THRESHOLD = 85.0


class ValidityCategory:
    category_name: str = _CATEGORY
    default_weight: int = 25
    category_rule_version: str = _RULE_VERSION

    def is_applicable(self, inputs: QualityEngineInput) -> bool:
        return inputs.validation_run.approved_changes_considered > 0

    def evaluate(self, inputs: QualityEngineInput) -> CategoryEvaluationResult:
        val_run = inputs.validation_run
        total = val_run.approved_changes_considered
        passed = val_run.passed_count
        failed = val_run.failed_count

        pass_rate = passed / total
        score = max(0.0, min(100.0, 100.0 * pass_rate))

        # All passed: perfect validity, no finding needed.
        if passed == total:
            return CategoryEvaluationResult(score=score, findings=())

        # Some changes did not pass (failed or skipped) — score may reflect
        # failures only, but any gap from 100% is reportable.
        if score < _FAIL_THRESHOLD:
            severity = "blocking"
            reason = CRITICAL_VALIDATION_FAILURE_RATE
        elif score < _WARN_THRESHOLD:
            severity = "warning"
            reason = HIGH_VALIDATION_FAILURE_RATE
        else:
            severity = "info"
            reason = VALIDATION_FAILURES_PRESENT

        # affected_row_count reports the actual failure count (not skipped),
        # since skipped changes are covered by validation_coverage category.
        finding = QualityFindingResult(
            category=_CATEGORY,
            rule_name=_RULE_NAME,
            rule_version=_RULE_VERSION,
            severity=severity,
            outcome="failed",
            reason=reason,
            affected_row_count=failed if failed > 0 else None,
        )
        return CategoryEvaluationResult(score=score, findings=(finding,))
