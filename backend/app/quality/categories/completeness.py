"""Completeness category — measures the proportion of non-missing cells in
the effective post-remediation dataset.

Score formula:
    if effective_row_count == 0:
        score = 0.0  (BLOCKING: EMPTY_DATASET)
    else:
        total_cells = effective_row_count * data_profile.column_count
        missing_rate = effective_missing_value_total / total_cells
        score = max(0.0, 100.0 * (1.0 - missing_rate))

Applicable when: DataProfile is always resolved by the handler (guaranteed);
the handler passes the DataProfile snapshot unconditionally. is_applicable()
returns True always for this category since the handler raises
PermanentExecutionError if the DataProfile is missing (Section 3.4).

Special case: if effective_row_count == 0 the category IS applicable but
produces a BLOCKING finding (EMPTY_DATASET) with score = 0.0.  This ensures
the zero-row case is caught even if all other categories are skipped.
See Section 11 of the architecture document.

Default weight: 20 (Section 6.1).
"""
from __future__ import annotations

from app.quality.reasons import (
    CRITICAL_MISSING_RATE,
    EMPTY_DATASET,
    HIGH_MISSING_RATE,
    MISSING_VALUES_PRESENT,
)
from app.quality.types import (
    CategoryEvaluationResult,
    QualityEngineInput,
    QualityFindingResult,
)

_CATEGORY = "completeness"
_RULE_NAME = "completeness_missing_value_rate"
_RULE_VERSION = "1.0"

# Internal score bands (architecture Section 8.3).
_FAIL_THRESHOLD = 60.0
_WARN_THRESHOLD = 85.0


class CompletenessCategory:
    category_name: str = _CATEGORY
    default_weight: int = 20
    category_rule_version: str = _RULE_VERSION

    def is_applicable(self, inputs: QualityEngineInput) -> bool:
        # DataProfile is always present (handler raises otherwise).
        # Zero-row datasets are still applicable — they produce EMPTY_DATASET.
        return True

    def evaluate(self, inputs: QualityEngineInput) -> CategoryEvaluationResult:
        effective_row_count = inputs.effective_stats.effective_row_count

        if effective_row_count == 0:
            finding = QualityFindingResult(
                category=_CATEGORY,
                rule_name=_RULE_NAME,
                rule_version=_RULE_VERSION,
                severity="blocking",
                outcome="failed",
                reason=EMPTY_DATASET,
                affected_row_count=0,
            )
            return CategoryEvaluationResult(score=0.0, findings=(finding,))

        column_count = inputs.data_profile.column_count
        effective_missing = inputs.effective_stats.effective_missing_value_total
        total_cells = effective_row_count * column_count

        # Guard against column_count == 0 (impossible per DB CHECK, but defensive).
        if total_cells <= 0:
            missing_rate = 0.0
        else:
            missing_rate = effective_missing / total_cells

        score = max(0.0, min(100.0, 100.0 * (1.0 - missing_rate)))

        if missing_rate == 0.0:
            # Perfect completeness — no finding needed.
            return CategoryEvaluationResult(score=score, findings=())

        if score < _FAIL_THRESHOLD:
            severity = "blocking"
            reason = CRITICAL_MISSING_RATE
        elif score < _WARN_THRESHOLD:
            severity = "warning"
            reason = HIGH_MISSING_RATE
        else:
            severity = "info"
            reason = MISSING_VALUES_PRESENT

        finding = QualityFindingResult(
            category=_CATEGORY,
            rule_name=_RULE_NAME,
            rule_version=_RULE_VERSION,
            severity=severity,
            outcome="failed",
            reason=reason,
            affected_row_count=effective_missing,
        )
        return CategoryEvaluationResult(score=score, findings=(finding,))
