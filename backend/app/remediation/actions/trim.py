"""trim_whitespace: handles both LEADING_WHITESPACE and TRAILING_WHITESPACE
Issue types with identical logic -- Module 14 already computed the exact,
unambiguous mechanical repair as Issue.suggested_fix (see
app.detection.rules.whitespace), so this action recomputes nothing at
all. Zero recomputation, zero drift risk between what Module 14 flagged
and what Module 15 proposes."""
from __future__ import annotations

from app.detection.issue_types import LEADING_WHITESPACE, TRAILING_WHITESPACE
from app.remediation.reasons import CHANGE_REASONS
from app.remediation.skip_reasons import NO_DETERMINISTIC_CHANGE_AVAILABLE
from app.remediation.types import (
    RemediationColumnConfig,
    RemediationDatasetConfigInput,
    RemediationIssueInput,
    RuleOutcome,
)

ACTION = "trim_whitespace"


def propose_trim(issue: RemediationIssueInput) -> RuleOutcome:
    if not issue.suggested_fix or issue.suggested_fix == issue.original_value:
        return RuleOutcome(changed=False, skip_reason=NO_DETERMINISTIC_CHANGE_AVAILABLE)
    return RuleOutcome(
        changed=True, proposed_value=issue.suggested_fix, reason=CHANGE_REASONS[ACTION]
    )


class TrimWhitespaceRule:
    issue_types = (LEADING_WHITESPACE, TRAILING_WHITESPACE)
    action = ACTION

    def propose(
        self,
        issue: RemediationIssueInput,
        column_config: RemediationColumnConfig | None,
        dataset_config: RemediationDatasetConfigInput,
    ) -> RuleOutcome:
        return propose_trim(issue)
