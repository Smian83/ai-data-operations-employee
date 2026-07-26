"""validate_standardize_capitalization: validates RemediationChange proposals
for the "standardize_capitalization" action (handles
INCONSISTENT_CAPITALIZATION from Module 14).

Directly reuses the same casing functions (lower_case, upper_case,
title_case) from app.standardization.rules.casing that Module 15's own
standardize_capitalization action uses. Gated on
column_config.capitalization_target -- the same config gate Module 15
enforces (CAPITALIZATION_TARGET_NOT_CONFIGURED skip), so a missing target
produces a validation skip, not a failure.

Reuse pattern: import the same _APPLY dict shape from Module 15's
capitalization module, but defined locally here to avoid a cross-module
import of internal state. The function imports are shared directly."""
from __future__ import annotations

from app.models.remediation_column_rule import CAPITALIZATION_TARGETS
from app.standardization.rules.casing import lower_case, title_case, upper_case
from app.validation.reasons import (
    CAPITALIZATION_TARGET_NOT_CONFIGURED,
    ORIGINAL_VALUE_MISSING,
    PROPOSED_VALUE_MATCHES,
    PROPOSED_VALUE_MISMATCH,
)
from app.validation.types import (
    ValidationChangeInput,
    ValidationColumnConfig,
    ValidationRuleOutcome,
)

_APPLY = {
    "lower": lower_case,
    "upper": upper_case,
    "title": title_case,
}
assert set(_APPLY) == set(CAPITALIZATION_TARGETS), (
    "validate_standardize_capitalization._APPLY keys must match CAPITALIZATION_TARGETS"
)

RULE_NAME = "validate_standardize_capitalization"
ACTION = "standardize_capitalization"
RULE_VERSION = "1.0"


def _validate_capitalization(
    change: ValidationChangeInput,
    column_config: ValidationColumnConfig | None,
) -> ValidationRuleOutcome:
    target = column_config.capitalization_target if column_config else None
    if not target:
        return ValidationRuleOutcome(
            outcome="skipped", reason=CAPITALIZATION_TARGET_NOT_CONFIGURED
        )

    value = change.original_value
    if not value:
        return ValidationRuleOutcome(outcome="skipped", reason=ORIGINAL_VALUE_MISSING)

    canonical = _APPLY[target](value)
    if canonical == change.proposed_value:
        return ValidationRuleOutcome(outcome="passed", reason=PROPOSED_VALUE_MATCHES)
    return ValidationRuleOutcome(outcome="failed", reason=PROPOSED_VALUE_MISMATCH)


class ValidateStandardizeCapitalizationRule:
    rule_name = RULE_NAME
    action = ACTION
    rule_version = RULE_VERSION

    def validate(
        self,
        change: ValidationChangeInput,
        column_config: ValidationColumnConfig | None,
    ) -> ValidationRuleOutcome:
        return _validate_capitalization(change, column_config)
