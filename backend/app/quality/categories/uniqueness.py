"""Uniqueness category — measures the proportion of unique rows in the
effective post-remediation dataset.

Score formula:
    duplicate_rate = effective_duplicate_row_count / effective_row_count
    score = max(0.0, 100.0 * (1.0 - duplicate_rate))

Applicable when: DataProfile resolved AND effective_row_count > 0.
If effective_row_count == 0, the completeness category already emits a
BLOCKING finding — uniqueness is skipped to avoid redundant signals.

Default weight: 15 (Section 6.1).
"""
from __future__ import annotations

from app.quality.reasons import (
    CRITICAL_DUPLICATE_RATE,
    DUPLICATES_PRESENT,
    HIGH_DUPLICATE_RATE,
)
from app.quality.types import (
    CategoryEvaluationResult,
    QualityEngineInput,
    QualityFindingResult,
)

_CATEGORY = "uniqueness"
_RULE_NAME = "uniqueness_duplicate_row_rate"
_RULE_VERSION = "1.0"

_FAIL_THRESHOLD = 60.0
_WARN_THRESHOLD = 85.0


class UniquenessCategory:
    category_name: str = _CATEGORY
    default_weight: int = 15
    category_rule_version: str = _RULE_VERSION

    def is_applicable(self, inputs: QualityEngineInput) -> bool:
        return inputs.effective_stats.effective_row_count > 0

    def evaluate(self, inputs: QualityEngineInput) -> CategoryEvaluationResult:
        effective_row_count = inputs.effective_stats.effective_row_count
        effective_duplicates = inputs.effective_stats.effective_duplicate_row_count

        duplicate_rate = effective_duplicates / effective_row_count
        score = max(0.0, min(100.0, 100.0 * (1.0 - duplicate_rate)))

        if effective_duplicates == 0:
            return CategoryEvaluationResult(score=score, findings=())

        if score < _FAIL_THRESHOLD:
            severity = "blocking"
            reason = CRITICAL_DUPLICATE_RATE
        elif score < _WARN_THRESHOLD:
            severity = "warning"
            reason = HIGH_DUPLICATE_RATE
        else:
            severity = "info"
            reason = DUPLICATES_PRESENT

        finding = QualityFindingResult(
            category=_CATEGORY,
            rule_name=_RULE_NAME,
            rule_version=_RULE_VERSION,
            severity=severity,
            outcome="failed",
            reason=reason,
            affected_row_count=effective_duplicates,
        )
        return CategoryEvaluationResult(score=score, findings=(finding,))
