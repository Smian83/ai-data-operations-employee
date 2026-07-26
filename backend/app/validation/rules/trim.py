"""validate_trim_whitespace: validates RemediationChange proposals for
the "trim_whitespace" action (handles LEADING_WHITESPACE and
TRAILING_WHITESPACE issue types from Module 14).

Module 15's trim rule reuses Module 14's suggested_fix verbatim --
lstrip() for LEADING_WHITESPACE, rstrip() for TRAILING_WHITESPACE.
The RemediationChange row carries only the action ("trim_whitespace"),
not the original issue_type, so this validation rule accepts either
lstrip() or rstrip() of original_value as a valid proposed_value.
Both are the single, unambiguous mechanical repair the Module 14
whitespace detector computes (see app.detection.rules.whitespace).

No standardization function import needed: the expected values are
Python built-ins (.lstrip(), .rstrip()), matching the detection rule
exactly without duplicating any non-trivial logic."""
from __future__ import annotations

from app.validation.reasons import (
    ORIGINAL_VALUE_MISSING,
    PROPOSED_VALUE_MATCHES,
    PROPOSED_VALUE_MISMATCH,
)
from app.validation.types import (
    ValidationChangeInput,
    ValidationColumnConfig,
    ValidationRuleOutcome,
)

RULE_NAME = "validate_trim_whitespace"
ACTION = "trim_whitespace"
RULE_VERSION = "1.0"


def _validate_trim(
    change: ValidationChangeInput,
    column_config: ValidationColumnConfig | None,
) -> ValidationRuleOutcome:
    value = change.original_value
    if value is None:
        return ValidationRuleOutcome(outcome="skipped", reason=ORIGINAL_VALUE_MISSING)

    # Module 14 produces lstrip() for LEADING_WHITESPACE and rstrip()
    # for TRAILING_WHITESPACE. The RemediationChange does not carry the
    # original issue_type, so either is accepted as the valid proposal.
    valid_proposals = {value.lstrip(), value.rstrip()}
    if change.proposed_value in valid_proposals:
        return ValidationRuleOutcome(outcome="passed", reason=PROPOSED_VALUE_MATCHES)
    return ValidationRuleOutcome(outcome="failed", reason=PROPOSED_VALUE_MISMATCH)


class ValidateTrimWhitespaceRule:
    rule_name = RULE_NAME
    action = ACTION
    rule_version = RULE_VERSION

    def validate(
        self,
        change: ValidationChangeInput,
        column_config: ValidationColumnConfig | None,
    ) -> ValidationRuleOutcome:
        return _validate_trim(change, column_config)
