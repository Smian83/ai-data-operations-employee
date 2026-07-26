"""normalize_phone: handles INVALID_PHONE. Reuses
app.standardization.rules.contact.standardize_phone directly (Module 7)
-- gated on RemediationColumnRule.default_country (design doc Risk R2 /
decision: "never guess a country", same reasoning Module 7 itself already
established for this exact function). Without a configured country,
E.164 conversion is never attempted."""
from __future__ import annotations

from app.detection.issue_types import INVALID_PHONE
from app.remediation.reasons import CHANGE_REASONS
from app.remediation.skip_reasons import (
    DEFAULT_COUNTRY_NOT_CONFIGURED,
    NO_DETERMINISTIC_CHANGE_AVAILABLE,
)
from app.remediation.types import (
    RemediationColumnConfig,
    RemediationDatasetConfigInput,
    RemediationIssueInput,
    RuleOutcome,
)
from app.standardization.rules.contact import standardize_phone

ACTION = "normalize_phone"


def propose_normalize_phone(
    issue: RemediationIssueInput, column_config: RemediationColumnConfig | None
) -> RuleOutcome:
    country = column_config.default_country if column_config else None
    if not country:
        return RuleOutcome(changed=False, skip_reason=DEFAULT_COUNTRY_NOT_CONFIGURED)

    value = issue.original_value or ""
    new_value, rule_applied = standardize_phone(value, country)
    if rule_applied is None:
        return RuleOutcome(changed=False, skip_reason=NO_DETERMINISTIC_CHANGE_AVAILABLE)
    return RuleOutcome(changed=True, proposed_value=new_value, reason=CHANGE_REASONS[ACTION])


class NormalizePhoneRule:
    issue_types = (INVALID_PHONE,)
    action = ACTION

    def propose(
        self,
        issue: RemediationIssueInput,
        column_config: RemediationColumnConfig | None,
        dataset_config: RemediationDatasetConfigInput,
    ) -> RuleOutcome:
        return propose_normalize_phone(issue, column_config)
