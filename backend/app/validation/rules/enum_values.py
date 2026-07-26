"""validate_normalize_enum_value: validates RemediationChange proposals
for the "normalize_enum_value" action (handles INVALID_ENUM_VALUE from
Module 14).

Directly mirrors Module 15's propose_normalize_enum_value logic
(app.remediation.actions.enum_values): case-insensitive + strip match
against allowed_values, accepting a proposal only when there is
EXACTLY ONE match. Gated on column_config.allowed_values
(ALLOWED_VALUES_NOT_CONFIGURED skip) -- same gate Module 15 enforces.

The deduplication+sorting of match candidates is identical to Module 15:
sorted set comprehension ensures the "exactly one match" test never
depends on allowed_values iteration order."""
from __future__ import annotations

from app.validation.reasons import (
    ALLOWED_VALUES_NOT_CONFIGURED,
    ORIGINAL_VALUE_MISSING,
    PROPOSED_VALUE_MATCHES,
    PROPOSED_VALUE_MISMATCH,
)
from app.validation.types import (
    ValidationChangeInput,
    ValidationColumnConfig,
    ValidationRuleOutcome,
)

RULE_NAME = "validate_normalize_enum_value"
ACTION = "normalize_enum_value"
RULE_VERSION = "1.0"


def _validate_enum_value(
    change: ValidationChangeInput,
    column_config: ValidationColumnConfig | None,
) -> ValidationRuleOutcome:
    allowed_values = column_config.allowed_values if column_config else None
    if not allowed_values:
        return ValidationRuleOutcome(
            outcome="skipped", reason=ALLOWED_VALUES_NOT_CONFIGURED
        )

    value = change.original_value
    if not value:
        return ValidationRuleOutcome(outcome="skipped", reason=ORIGINAL_VALUE_MISSING)

    target = value.strip().casefold()
    # Deduplicated + sorted -- identical to Module 15 to ensure
    # deterministic "exactly one match" test regardless of declaration order.
    matches = sorted({
        allowed for allowed in allowed_values
        if allowed.strip().casefold() == target
    })
    if len(matches) != 1:
        # Zero or ambiguous matches: Module 15 would have skipped this
        # issue, so a RemediationChange proposing a replacement is
        # inconsistent with what the rule would produce today.
        return ValidationRuleOutcome(outcome="failed", reason=PROPOSED_VALUE_MISMATCH)

    canonical = matches[0]
    if canonical == change.proposed_value:
        return ValidationRuleOutcome(outcome="passed", reason=PROPOSED_VALUE_MATCHES)
    return ValidationRuleOutcome(outcome="failed", reason=PROPOSED_VALUE_MISMATCH)


class ValidateNormalizeEnumValueRule:
    rule_name = RULE_NAME
    action = ACTION
    rule_version = RULE_VERSION

    def validate(
        self,
        change: ValidationChangeInput,
        column_config: ValidationColumnConfig | None,
    ) -> ValidationRuleOutcome:
        return _validate_enum_value(change, column_config)
