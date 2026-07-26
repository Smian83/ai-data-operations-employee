"""validate_normalize_date: validates RemediationChange proposals for
the "normalize_date" action (handles INVALID_DATE from Module 14).

Directly mirrors Module 15's propose_normalize_date logic
(app.remediation.actions.dates): re-parses original_value using
source_date_format and re-renders using target_date_format, then
compares to change.proposed_value. Both formats must be configured
(same two-gate requirement Module 15 enforces:
SOURCE_DATE_FORMAT_NOT_CONFIGURED / TARGET_DATE_FORMAT_NOT_CONFIGURED).

Deliberately NOT reusing app.standardization.rules.temporal.standardize_
date -- same rationale as Module 15 (Risk R1 in the Module 15 design
doc): that function is strictly datetime.fromisoformat-based and would
almost always produce a no-op for values Module 14 already flagged as
invalid. The explicit strptime/strftime pair is the correct approach."""
from __future__ import annotations

from datetime import datetime

from app.validation.reasons import (
    PROPOSED_VALUE_MISMATCH,
    PROPOSED_VALUE_MATCHES,
    SOURCE_DATE_FORMAT_NOT_CONFIGURED,
    TARGET_DATE_FORMAT_NOT_CONFIGURED,
    ORIGINAL_VALUE_MISSING,
)
from app.validation.types import (
    ValidationChangeInput,
    ValidationColumnConfig,
    ValidationRuleOutcome,
)

RULE_NAME = "validate_normalize_date"
ACTION = "normalize_date"
RULE_VERSION = "1.0"


def _validate_date(
    change: ValidationChangeInput,
    column_config: ValidationColumnConfig | None,
) -> ValidationRuleOutcome:
    source_format = column_config.source_date_format if column_config else None
    if not source_format:
        return ValidationRuleOutcome(
            outcome="skipped", reason=SOURCE_DATE_FORMAT_NOT_CONFIGURED
        )

    target_format = column_config.target_date_format if column_config else None
    if not target_format:
        return ValidationRuleOutcome(
            outcome="skipped", reason=TARGET_DATE_FORMAT_NOT_CONFIGURED
        )

    value = change.original_value
    if not value:
        return ValidationRuleOutcome(outcome="skipped", reason=ORIGINAL_VALUE_MISSING)

    try:
        parsed = datetime.strptime(value, source_format)
    except ValueError:
        # Cannot parse under the configured source format -- same
        # "does not match configured source format" skip as Module 15.
        return ValidationRuleOutcome(outcome="failed", reason=PROPOSED_VALUE_MISMATCH)

    canonical = parsed.strftime(target_format)
    if canonical == change.proposed_value:
        return ValidationRuleOutcome(outcome="passed", reason=PROPOSED_VALUE_MATCHES)
    return ValidationRuleOutcome(outcome="failed", reason=PROPOSED_VALUE_MISMATCH)


class ValidateNormalizeDateRule:
    rule_name = RULE_NAME
    action = ACTION
    rule_version = RULE_VERSION

    def validate(
        self,
        change: ValidationChangeInput,
        column_config: ValidationColumnConfig | None,
    ) -> ValidationRuleOutcome:
        return _validate_date(change, column_config)
