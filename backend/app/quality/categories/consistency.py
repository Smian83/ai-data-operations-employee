"""Consistency category — evaluates whether normalization rules were applied
uniformly across column values (Section 6.2 of the architecture document).

Applicable when: at least one of the four consistency-related validation rule
names has results in ValidationRun.results_by_rule:
    validate_standardize_capitalization
    validate_normalize_boolean
    validate_normalize_date
    validate_normalize_enum_value

These rules are the only ones where "column consistency" (all values in a
column treated the same way) is testable from ValidationResult outcomes. If
a column has both passed and failed results for the same normalization rule,
that column is inconsistently normalized.

Score formula:
    inconsistent_pairs = number of (rule, column_name) pairs where
        both "passed" and "failed" outcomes exist in the ValidationResults.
    total_pairs = number of distinct (rule, column_name) pairs for
        consistency-related rules.
    consistency_rate = 1.0 - (inconsistent_pairs / total_pairs)
    score = 100.0 * consistency_rate

One WARNING finding is emitted per inconsistent (rule, column_name) pair,
reporting INCONSISTENT_NORMALIZATION. No BLOCKING finding is emitted by this
category — inconsistency degrades the score but is not an outright blocker
(the score falling below fail_threshold still causes FAIL at the release level).

Default weight: 10 (Section 6.1).
"""
from __future__ import annotations

from app.quality.reasons import INCONSISTENT_NORMALIZATION
from app.quality.types import (
    CategoryEvaluationResult,
    QualityEngineInput,
    QualityFindingResult,
)

_CATEGORY = "consistency"
_RULE_VERSION = "1.0"

# The four validation rule names whose results can reveal column-level
# inconsistency. These must match VALIDATION_RULE_NAMES in enums.py exactly.
_CONSISTENCY_RULE_NAMES: frozenset[str] = frozenset({
    "validate_standardize_capitalization",
    "validate_normalize_boolean",
    "validate_normalize_date",
    "validate_normalize_enum_value",
})


class ConsistencyCategory:
    category_name: str = _CATEGORY
    default_weight: int = 10
    category_rule_version: str = _RULE_VERSION

    def is_applicable(self, inputs: QualityEngineInput) -> bool:
        """Return True if at least one consistency-related rule has results."""
        return any(
            rule_name in _CONSISTENCY_RULE_NAMES
            for rule_name in inputs.validation_run.results_by_rule
        )

    def evaluate(self, inputs: QualityEngineInput) -> CategoryEvaluationResult:
        # Group (rule_name, column_name) → set of outcomes.
        # column_name is None for row-level rules; exclude None (consistency
        # is a column-level concept — row-level rules cannot be inconsistent
        # in the column sense).
        pair_outcomes: dict[tuple[str, str], set[str]] = {}

        for result in inputs.validation_results:
            if result.rule_name not in _CONSISTENCY_RULE_NAMES:
                continue
            if result.column_name is None:
                continue
            key = (result.rule_name, result.column_name)
            if key not in pair_outcomes:
                pair_outcomes[key] = set()
            pair_outcomes[key].add(result.outcome)

        total_pairs = len(pair_outcomes)
        if total_pairs == 0:
            # results_by_rule said there were results, but none had a column
            # name — treat as fully consistent (vacuous pass).
            return CategoryEvaluationResult(score=100.0, findings=())

        # A pair is inconsistent when it has BOTH passed AND failed outcomes.
        # Skipped results alone do not constitute inconsistency.
        inconsistent: list[tuple[str, str]] = []
        for (rule_name, col_name), outcomes in pair_outcomes.items():
            if "passed" in outcomes and "failed" in outcomes:
                inconsistent.append((rule_name, col_name))

        # Stable sort for deterministic finding order.
        inconsistent.sort()

        consistency_rate = 1.0 - (len(inconsistent) / total_pairs)
        score = max(0.0, min(100.0, 100.0 * consistency_rate))

        findings: list[QualityFindingResult] = []
        for rule_name, col_name in inconsistent:
            findings.append(
                QualityFindingResult(
                    category=_CATEGORY,
                    rule_name=f"consistency_{rule_name}",
                    rule_version=_RULE_VERSION,
                    severity="warning",
                    outcome="failed",
                    reason=INCONSISTENT_NORMALIZATION,
                    affected_column=col_name,
                )
            )

        return CategoryEvaluationResult(score=score, findings=tuple(findings))
