"""Unresolved Risk category — measures whether HIGH and CRITICAL severity
Issues remain unaddressed after the remediation+validation pipeline.

Applicable when: IssueDetectionRun.total_issues_found > 0.
If no issues were detected, there is no risk signal (skipped).

Score formula:
    unresolved_high_critical = unresolved_high_count + unresolved_critical_count
    if unresolved_high_critical == 0:
        score = 100.0  (no finding)
    else:
        score = max(0.0, 100.0 * (1.0 - unresolved_high_critical
                                        / total_issues_found))

One finding per type of unresolved severity:
  - unresolved_critical_count > 0 → BLOCKING (UNRESOLVED_CRITICAL_ISSUES)
  - unresolved_high_count > 0     → WARNING  (UNRESOLVED_HIGH_ISSUES)

The global FAIL conditions for unresolved severity counts (Section 10,
conditions 5 and 6) are evaluated at the release-decision level by the engine,
not by this category. This category produces its own score and severity-based
findings — those findings trigger the FAIL via blocking_count > 0 (condition 2)
if critical issues are unresolved, which is the correct ordering.

Default weight: 25 (Section 6.1).
"""
from __future__ import annotations

from app.quality.reasons import UNRESOLVED_CRITICAL_ISSUES, UNRESOLVED_HIGH_ISSUES
from app.quality.types import (
    CategoryEvaluationResult,
    QualityEngineInput,
    QualityFindingResult,
)

_CATEGORY = "unresolved_risk"
_RULE_NAME_CRITICAL = "unresolved_risk_critical_severity"
_RULE_NAME_HIGH = "unresolved_risk_high_severity"
_RULE_VERSION = "1.0"


class UnresolvedRiskCategory:
    category_name: str = _CATEGORY
    default_weight: int = 25
    category_rule_version: str = _RULE_VERSION

    def is_applicable(self, inputs: QualityEngineInput) -> bool:
        return inputs.issue_detection_run.total_issues_found > 0

    def evaluate(self, inputs: QualityEngineInput) -> CategoryEvaluationResult:
        stats = inputs.effective_stats
        unresolved_critical = stats.unresolved_critical_count
        unresolved_high = stats.unresolved_high_count
        total_issues = inputs.issue_detection_run.total_issues_found

        unresolved_high_critical = unresolved_critical + unresolved_high

        if unresolved_high_critical == 0:
            return CategoryEvaluationResult(score=100.0, findings=())

        score = max(
            0.0,
            min(
                100.0,
                100.0 * (1.0 - unresolved_high_critical / max(1, total_issues)),
            ),
        )

        findings: list[QualityFindingResult] = []

        # BLOCKING: critical severity always emitted first (stable order).
        if unresolved_critical > 0:
            findings.append(
                QualityFindingResult(
                    category=_CATEGORY,
                    rule_name=_RULE_NAME_CRITICAL,
                    rule_version=_RULE_VERSION,
                    severity="blocking",
                    outcome="failed",
                    reason=UNRESOLVED_CRITICAL_ISSUES,
                    affected_row_count=unresolved_critical,
                )
            )

        # WARNING: high severity.
        if unresolved_high > 0:
            findings.append(
                QualityFindingResult(
                    category=_CATEGORY,
                    rule_name=_RULE_NAME_HIGH,
                    rule_version=_RULE_VERSION,
                    severity="warning",
                    outcome="failed",
                    reason=UNRESOLVED_HIGH_ISSUES,
                    affected_row_count=unresolved_high,
                )
            )

        return CategoryEvaluationResult(score=score, findings=tuple(findings))
