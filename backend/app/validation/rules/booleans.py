"""validate_normalize_boolean: validates RemediationChange proposals for
the "normalize_boolean" action (handles BOOLEAN_INCONSISTENCY from
Module 14).

Directly reuses app.standardization.rules.values.standardize_boolean --
the same function Module 15's normalize_boolean action calls (see
app.remediation.actions.booleans). No config gate (same as Module 15:
"none -- always proposed when the function returns a change"). Re-runs
the function with output_form=None and compares its output to
change.proposed_value."""
from __future__ import annotations

from app.standardization.rules.values import standardize_boolean
from app.validation.reasons import (
    PROPOSED_VALUE_MATCHES,
    PROPOSED_VALUE_MISMATCH,
)
from app.validation.types import (
    ValidationChangeInput,
    ValidationColumnConfig,
    ValidationRuleOutcome,
)

RULE_NAME = "validate_normalize_boolean"
ACTION = "normalize_boolean"
RULE_VERSION = "1.0"


def _validate_boolean(
    change: ValidationChangeInput,
    column_config: ValidationColumnConfig | None,
) -> ValidationRuleOutcome:
    value = change.original_value or ""
    new_value, rule_applied = standardize_boolean(value, output_form=None)
    if rule_applied is None:
        # Function says this value has no canonical boolean form, so a
        # RemediationChange proposing a replacement is inconsistent.
        return ValidationRuleOutcome(outcome="failed", reason=PROPOSED_VALUE_MISMATCH)
    if change.proposed_value == new_value:
        return ValidationRuleOutcome(outcome="passed", reason=PROPOSED_VALUE_MATCHES)
    return ValidationRuleOutcome(outcome="failed", reason=PROPOSED_VALUE_MISMATCH)


class ValidateNormalizeBooleanRule:
    rule_name = RULE_NAME
    action = ACTION
    rule_version = RULE_VERSION

    def validate(
        self,
        change: ValidationChangeInput,
        column_config: ValidationColumnConfig | None,
    ) -> ValidationRuleOutcome:
        return _validate_boolean(change, column_config)
