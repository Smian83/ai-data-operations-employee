"""validate_normalize_numeric: validates RemediationChange proposals for
the "normalize_numeric" action (handles INVALID_NUMERIC from Module 14).

Directly reuses app.standardization.rules.values.standardize_numeric --
the same function Module 15's normalize_numeric action calls. No config
gate (same as Module 15: "none -- function itself is conservative").
locale=None is passed here exactly as Module 15 passes it, preserving
the most-conservative disambiguation mode."""
from __future__ import annotations

from app.standardization.rules.values import standardize_numeric
from app.validation.reasons import (
    PROPOSED_VALUE_MATCHES,
    PROPOSED_VALUE_MISMATCH,
)
from app.validation.types import (
    ValidationChangeInput,
    ValidationColumnConfig,
    ValidationRuleOutcome,
)

RULE_NAME = "validate_normalize_numeric"
ACTION = "normalize_numeric"
RULE_VERSION = "1.0"


def _validate_numeric(
    change: ValidationChangeInput,
    column_config: ValidationColumnConfig | None,
) -> ValidationRuleOutcome:
    value = change.original_value or ""
    new_value, rule_applied = standardize_numeric(value, locale=None)
    if rule_applied is None:
        # Function says this value has no canonical numeric form -- a
        # RemediationChange proposing a replacement is inconsistent.
        return ValidationRuleOutcome(outcome="failed", reason=PROPOSED_VALUE_MISMATCH)
    if change.proposed_value == new_value:
        return ValidationRuleOutcome(outcome="passed", reason=PROPOSED_VALUE_MATCHES)
    return ValidationRuleOutcome(outcome="failed", reason=PROPOSED_VALUE_MISMATCH)


class ValidateNormalizeNumericRule:
    rule_name = RULE_NAME
    action = ACTION
    rule_version = RULE_VERSION

    def validate(
        self,
        change: ValidationChangeInput,
        column_config: ValidationColumnConfig | None,
    ) -> ValidationRuleOutcome:
        return _validate_numeric(change, column_config)
