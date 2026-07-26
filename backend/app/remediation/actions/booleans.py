"""normalize_boolean: handles BOOLEAN_INCONSISTENCY. Reuses
app.standardization.rules.values.standardize_boolean directly (Module 7)
-- no config gate at all (design doc: "none -- always proposed when the
function returns a change"), since that function's own (value, None)
convention already leaves ambiguous/unrecognized tokens untouched, which
is exactly Module 15's own "never guess" requirement satisfied for free
by the reused function."""
from __future__ import annotations

from app.detection.issue_types import BOOLEAN_INCONSISTENCY
from app.remediation.reasons import CHANGE_REASONS
from app.remediation.skip_reasons import NO_DETERMINISTIC_CHANGE_AVAILABLE
from app.remediation.types import (
    RemediationColumnConfig,
    RemediationDatasetConfigInput,
    RemediationIssueInput,
    RuleOutcome,
)
from app.standardization.rules.values import standardize_boolean

ACTION = "normalize_boolean"


def propose_normalize_boolean(issue: RemediationIssueInput) -> RuleOutcome:
    value = issue.original_value or ""
    _new_value, rule_applied = standardize_boolean(value, output_form=None)
    if rule_applied is None:
        return RuleOutcome(changed=False, skip_reason=NO_DETERMINISTIC_CHANGE_AVAILABLE)
    return RuleOutcome(changed=True, proposed_value=_new_value, reason=CHANGE_REASONS[ACTION])


class NormalizeBooleanRule:
    issue_types = (BOOLEAN_INCONSISTENCY,)
    action = ACTION

    def propose(
        self,
        issue: RemediationIssueInput,
        column_config: RemediationColumnConfig | None,
        dataset_config: RemediationDatasetConfigInput,
    ) -> RuleOutcome:
        return propose_normalize_boolean(issue)
