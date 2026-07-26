"""normalize_enum_value: handles INVALID_ENUM_VALUE. New logic -- gated on
IssueDetectionColumnRule.allowed_values (Module 14's own config table,
read directly here via RemediationColumnConfig.allowed_values, never
duplicated into RemediationColumnRule -- see that dataclass's own
docstring). Module 14's detector (app.detection.rules.enum_values) only
ever flags a value that fails an EXACT, case-sensitive match against
allowed_values; this action asks a narrower question of that same
already-failed value: does it match exactly one allowed value once case
and surrounding whitespace are ignored? A proposal is made only when
there is exactly one such candidate -- zero matches is left alone (still
genuinely invalid), and more than one match is left alone too (which one
was "meant" would be a guess, never made)."""
from __future__ import annotations

from app.detection.issue_types import INVALID_ENUM_VALUE
from app.remediation.reasons import CHANGE_REASONS
from app.remediation.skip_reasons import (
    ALLOWED_VALUES_NOT_CONFIGURED,
    NO_DETERMINISTIC_CHANGE_AVAILABLE,
)
from app.remediation.types import (
    RemediationColumnConfig,
    RemediationDatasetConfigInput,
    RemediationIssueInput,
    RuleOutcome,
)

ACTION = "normalize_enum_value"


def propose_normalize_enum_value(
    issue: RemediationIssueInput, column_config: RemediationColumnConfig | None
) -> RuleOutcome:
    allowed_values = column_config.allowed_values if column_config else None
    if not allowed_values:
        return RuleOutcome(changed=False, skip_reason=ALLOWED_VALUES_NOT_CONFIGURED)

    value = issue.original_value
    if not value:
        return RuleOutcome(changed=False, skip_reason=NO_DETERMINISTIC_CHANGE_AVAILABLE)

    target = value.strip().casefold()
    # Deduplicated + sorted so the candidate set (and therefore the
    # "exactly one match" test) never depends on allowed_values' own
    # iteration/declaration order, and an accidental duplicate entry in
    # config (two allowed values differing only by case) cannot silently
    # inflate the match count.
    matches = sorted({
        allowed for allowed in allowed_values if allowed.strip().casefold() == target
    })
    if len(matches) != 1:
        return RuleOutcome(changed=False, skip_reason=NO_DETERMINISTIC_CHANGE_AVAILABLE)

    canonical = matches[0]
    if canonical == value:
        return RuleOutcome(changed=False, skip_reason=NO_DETERMINISTIC_CHANGE_AVAILABLE)
    return RuleOutcome(changed=True, proposed_value=canonical, reason=CHANGE_REASONS[ACTION])


class NormalizeEnumValueRule:
    issue_types = (INVALID_ENUM_VALUE,)
    action = ACTION

    def propose(
        self,
        issue: RemediationIssueInput,
        column_config: RemediationColumnConfig | None,
        dataset_config: RemediationDatasetConfigInput,
    ) -> RuleOutcome:
        return propose_normalize_enum_value(issue, column_config)
