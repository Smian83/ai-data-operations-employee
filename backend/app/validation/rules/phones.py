"""validate_normalize_phone: validates RemediationChange proposals for
the "normalize_phone" action (handles INVALID_PHONE from Module 14).

Directly reuses app.standardization.rules.contact.standardize_phone --
the same function Module 15's normalize_phone action calls. Gated on
column_config.default_country (DEFAULT_COUNTRY_NOT_CONFIGURED skip)
-- same gate Module 15 enforces ("never guess a country")."""
from __future__ import annotations

from app.standardization.rules.contact import standardize_phone
from app.validation.reasons import (
    DEFAULT_COUNTRY_NOT_CONFIGURED,
    PROPOSED_VALUE_MATCHES,
    PROPOSED_VALUE_MISMATCH,
)
from app.validation.types import (
    ValidationChangeInput,
    ValidationColumnConfig,
    ValidationRuleOutcome,
)

RULE_NAME = "validate_normalize_phone"
ACTION = "normalize_phone"
RULE_VERSION = "1.0"


def _validate_phone(
    change: ValidationChangeInput,
    column_config: ValidationColumnConfig | None,
) -> ValidationRuleOutcome:
    country = column_config.default_country if column_config else None
    if not country:
        return ValidationRuleOutcome(
            outcome="skipped", reason=DEFAULT_COUNTRY_NOT_CONFIGURED
        )

    value = change.original_value or ""
    new_value, rule_applied = standardize_phone(value, country)
    if rule_applied is None:
        # Function cannot produce a canonical E.164 form for this value --
        # a RemediationChange proposing a replacement is inconsistent.
        return ValidationRuleOutcome(outcome="failed", reason=PROPOSED_VALUE_MISMATCH)
    if change.proposed_value == new_value:
        return ValidationRuleOutcome(outcome="passed", reason=PROPOSED_VALUE_MATCHES)
    return ValidationRuleOutcome(outcome="failed", reason=PROPOSED_VALUE_MISMATCH)


class ValidateNormalizePhoneRule:
    rule_name = RULE_NAME
    action = ACTION
    rule_version = RULE_VERSION

    def validate(
        self,
        change: ValidationChangeInput,
        column_config: ValidationColumnConfig | None,
    ) -> ValidationRuleOutcome:
        return _validate_phone(change, column_config)
