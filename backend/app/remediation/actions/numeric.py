"""normalize_numeric: handles INVALID_NUMERIC. Reuses
app.standardization.rules.values.standardize_numeric directly (Module 7)
-- no config gate (design doc: "none -- function itself is conservative").
Module 15 never configures a numeric_locale of its own (unlike
StandardizationConfig.numeric_locale in Module 7): locale is always None
here, which is the function's own most-conservative disambiguation mode
-- see standardize_numeric's docstring for exactly which comma/dot shapes
that leaves untouched rather than guessing."""
from __future__ import annotations

from app.detection.issue_types import INVALID_NUMERIC
from app.remediation.reasons import CHANGE_REASONS
from app.remediation.skip_reasons import NO_DETERMINISTIC_CHANGE_AVAILABLE
from app.remediation.types import (
    RemediationColumnConfig,
    RemediationDatasetConfigInput,
    RemediationIssueInput,
    RuleOutcome,
)
from app.standardization.rules.values import standardize_numeric

ACTION = "normalize_numeric"


def propose_normalize_numeric(issue: RemediationIssueInput) -> RuleOutcome:
    value = issue.original_value or ""
    new_value, rule_applied = standardize_numeric(value, locale=None)
    if rule_applied is None:
        return RuleOutcome(changed=False, skip_reason=NO_DETERMINISTIC_CHANGE_AVAILABLE)
    return RuleOutcome(changed=True, proposed_value=new_value, reason=CHANGE_REASONS[ACTION])


class NormalizeNumericRule:
    issue_types = (INVALID_NUMERIC,)
    action = ACTION

    def propose(
        self,
        issue: RemediationIssueInput,
        column_config: RemediationColumnConfig | None,
        dataset_config: RemediationDatasetConfigInput,
    ) -> RuleOutcome:
        return propose_normalize_numeric(issue)
