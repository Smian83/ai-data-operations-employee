"""validate_collapse_multiple_spaces: validates RemediationChange proposals
for the "collapse_multiple_spaces" action (handles
MULTIPLE_INTERNAL_SPACES from Module 14).

Module 15's collapse rule reuses Module 14's suggested_fix verbatim --
the collapsed form produced by the same regex the MultipleInternalSpacesRule
detector uses (see app.detection.rules.whitespace). This rule re-applies
that exact regex to original_value and compares the result to
proposed_value.

The regex `r"(\\S)\\s{2,}(\\S)"` with replacement `r"\\1 \\2"` collapses
runs of 2+ whitespace characters between non-whitespace characters to a
single space. It does NOT strip leading/trailing whitespace (that is the
trim_whitespace action) and does NOT collapse whitespace at the very
start or end of the string. The same regex is used here as in
app.detection.rules.whitespace.MultipleInternalSpacesRule.detect() to
ensure identical deterministic output."""
from __future__ import annotations

import re

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

# Identical to app.detection.rules.whitespace.MultipleInternalSpacesRule.
_INTERNAL_MULTISPACE_RE = re.compile(r"(\S)\s{2,}(\S)")

RULE_NAME = "validate_collapse_multiple_spaces"
ACTION = "collapse_multiple_spaces"
RULE_VERSION = "1.0"


def _validate_collapse(
    change: ValidationChangeInput,
    column_config: ValidationColumnConfig | None,
) -> ValidationRuleOutcome:
    value = change.original_value
    if value is None:
        return ValidationRuleOutcome(outcome="skipped", reason=ORIGINAL_VALUE_MISSING)

    expected = _INTERNAL_MULTISPACE_RE.sub(r"\1 \2", value)
    if change.proposed_value == expected:
        return ValidationRuleOutcome(outcome="passed", reason=PROPOSED_VALUE_MATCHES)
    return ValidationRuleOutcome(outcome="failed", reason=PROPOSED_VALUE_MISMATCH)


class ValidateCollapseMultipleSpacesRule:
    rule_name = RULE_NAME
    action = ACTION
    rule_version = RULE_VERSION

    def validate(
        self,
        change: ValidationChangeInput,
        column_config: ValidationColumnConfig | None,
    ) -> ValidationRuleOutcome:
        return _validate_collapse(change, column_config)
