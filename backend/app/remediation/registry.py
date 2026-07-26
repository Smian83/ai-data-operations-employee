"""REMEDIATION_RULES: every registered remediation action, in a fixed
declaration order (app.models.enums.REMEDIATION_ACTIONS' own order).
Adding a new action is exactly two steps, same as app.detection.registry:
  1. write the propose_ function + Rule class in app.remediation.actions
     (implementing app.remediation.base.RemediationRule);
  2. add one line to the tuple below.
Nothing in app.remediation.engine ever needs to change -- it only ever
knows "look up the rule for this Issue's issue_type, call .propose()".

Unlike DETECTION_RULES (which the engine iterates in full against a whole
dataset), remediation dispatches per-Issue by issue_type -- so this module
also builds _RULES_BY_ISSUE_TYPE, an O(1) issue_type -> rule lookup, via
get_rule_for_issue_type(). A rule with no entry for a given issue_type
means that issue_type has no Module 15 action at all (missing_value,
empty_string, null_value, invalid_email, required_field_violation,
outlier, broken_fk_reference -- see the design doc Section 4's explicit
exclusion list); the engine treats that as
skip_reasons.ISSUE_TYPE_NOT_REMEDIABLE, never an error."""
from app.remediation.actions.booleans import NormalizeBooleanRule
from app.remediation.actions.capitalization import StandardizeCapitalizationRule
from app.remediation.actions.collapse import CollapseMultipleSpacesRule
from app.remediation.actions.dates import NormalizeDateRule
from app.remediation.actions.duplicates import (
    RemoveDuplicatePrimaryKeyRule,
    RemoveDuplicateRowRule,
)
from app.remediation.actions.enum_values import NormalizeEnumValueRule
from app.remediation.actions.numeric import NormalizeNumericRule
from app.remediation.actions.phones import NormalizePhoneRule
from app.remediation.actions.trim import TrimWhitespaceRule
from app.remediation.base import RemediationRule

REMEDIATION_RULES: tuple[RemediationRule, ...] = (
    TrimWhitespaceRule(),
    CollapseMultipleSpacesRule(),
    StandardizeCapitalizationRule(),
    NormalizeBooleanRule(),
    NormalizeDateRule(),
    NormalizePhoneRule(),
    NormalizeNumericRule(),
    NormalizeEnumValueRule(),
    RemoveDuplicateRowRule(),
    RemoveDuplicatePrimaryKeyRule(),
)

assert len(REMEDIATION_RULES) == 10
assert len({rule.action for rule in REMEDIATION_RULES}) == 10, (
    "two or more registered rules share an action -- each rule in "
    "REMEDIATION_RULES must own a distinct action"
)

_RULES_BY_ISSUE_TYPE: dict[str, RemediationRule] = {}
for _rule in REMEDIATION_RULES:
    for _issue_type in _rule.issue_types:
        assert _issue_type not in _RULES_BY_ISSUE_TYPE, (
            f"duplicate issue_type mapping: {_issue_type!r} is claimed by both "
            f"{_RULES_BY_ISSUE_TYPE[_issue_type].action!r} and {_rule.action!r}"
        )
        _RULES_BY_ISSUE_TYPE[_issue_type] = _rule
# 11 issue_types map to the 10 actions above (trim_whitespace alone
# handles two: leading_whitespace and trailing_whitespace).
assert len(_RULES_BY_ISSUE_TYPE) == 11


def get_rule_for_issue_type(issue_type: str) -> RemediationRule | None:
    """None means this issue_type has no Module 15 action at all -- the
    engine's caller must treat that as a skip, never an error."""
    return _RULES_BY_ISSUE_TYPE.get(issue_type)
