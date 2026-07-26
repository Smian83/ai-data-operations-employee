"""collapse_multiple_spaces: handles MULTIPLE_INTERNAL_SPACES. Same
zero-recomputation shape as trim.py -- Module 14's
app.detection.rules.whitespace.MultipleInternalSpacesRule already computed
the single, unambiguous collapsed form as Issue.suggested_fix."""
from __future__ import annotations

from app.detection.issue_types import MULTIPLE_INTERNAL_SPACES
from app.remediation.reasons import CHANGE_REASONS
from app.remediation.skip_reasons import NO_DETERMINISTIC_CHANGE_AVAILABLE
from app.remediation.types import (
    RemediationColumnConfig,
    RemediationDatasetConfigInput,
    RemediationIssueInput,
    RuleOutcome,
)

ACTION = "collapse_multiple_spaces"


def propose_collapse(issue: RemediationIssueInput) -> RuleOutcome:
    if not issue.suggested_fix or issue.suggested_fix == issue.original_value:
        return RuleOutcome(changed=False, skip_reason=NO_DETERMINISTIC_CHANGE_AVAILABLE)
    return RuleOutcome(
        changed=True, proposed_value=issue.suggested_fix, reason=CHANGE_REASONS[ACTION]
    )


class CollapseMultipleSpacesRule:
    issue_types = (MULTIPLE_INTERNAL_SPACES,)
    action = ACTION

    def propose(
        self,
        issue: RemediationIssueInput,
        column_config: RemediationColumnConfig | None,
        dataset_config: RemediationDatasetConfigInput,
    ) -> RuleOutcome:
        return propose_collapse(issue)
