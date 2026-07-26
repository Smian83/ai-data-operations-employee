"""standardize_capitalization: handles INCONSISTENT_CAPITALIZATION. Reuses
app.standardization.rules.casing's own lower_case/upper_case/title_case
primitives -- Module 15 Phase 5 correction: this action originally used
Python's built-in str.title() for the "title" target, which reintroduced
the exact apostrophe bug casing.title_case() was written to avoid (e.g.
"don't".title() == "Don'T"; casing.py's own docstring documents this).
Reusing the shared primitive keeps capitalization behavior identical
everywhere in the system a "title case" operation happens, Module 7's
StandardizationHandler included -- the same "reuse the existing
deterministic transformation, never a second implementation" rule every
other remediation action in this package already follows (booleans.py,
numeric.py, phones.py). Module 14's own "dominant pattern" detection is
never treated as the target (approved Module 15 decision: an explicit
RemediationColumnRule.capitalization_target is required, never inferred).
Gated: no proposal without a configured target, per the design doc's
"never guess capitalization" requirement."""
from __future__ import annotations

from app.detection.issue_types import INCONSISTENT_CAPITALIZATION
from app.models.remediation_column_rule import CAPITALIZATION_TARGETS
from app.remediation.reasons import CHANGE_REASONS
from app.remediation.skip_reasons import (
    CAPITALIZATION_TARGET_NOT_CONFIGURED,
    NO_DETERMINISTIC_CHANGE_AVAILABLE,
)
from app.remediation.types import (
    RemediationColumnConfig,
    RemediationDatasetConfigInput,
    RemediationIssueInput,
    RuleOutcome,
)
from app.standardization.rules.casing import lower_case, title_case, upper_case

ACTION = "standardize_capitalization"

_APPLY = {
    "lower": lower_case,
    "upper": upper_case,
    "title": title_case,
}
assert set(_APPLY) == set(CAPITALIZATION_TARGETS)


def propose_standardize_capitalization(
    issue: RemediationIssueInput, column_config: RemediationColumnConfig | None
) -> RuleOutcome:
    target = column_config.capitalization_target if column_config else None
    if not target:
        return RuleOutcome(changed=False, skip_reason=CAPITALIZATION_TARGET_NOT_CONFIGURED)

    value = issue.original_value
    if not value:
        return RuleOutcome(changed=False, skip_reason=NO_DETERMINISTIC_CHANGE_AVAILABLE)

    canonical = _APPLY[target](value)
    if canonical == value:
        return RuleOutcome(changed=False, skip_reason=NO_DETERMINISTIC_CHANGE_AVAILABLE)
    return RuleOutcome(changed=True, proposed_value=canonical, reason=CHANGE_REASONS[ACTION])


class StandardizeCapitalizationRule:
    issue_types = (INCONSISTENT_CAPITALIZATION,)
    action = ACTION

    def propose(
        self,
        issue: RemediationIssueInput,
        column_config: RemediationColumnConfig | None,
        dataset_config: RemediationDatasetConfigInput,
    ) -> RuleOutcome:
        return propose_standardize_capitalization(issue, column_config)
