"""VALIDATION_RULES: every registered validation rule, in
app.models.enums.REMEDIATION_ACTIONS' own declaration order (one rule
per action). Adding a new rule is exactly two steps, same as
app.remediation.registry:
  1. write the rule class in app.validation.rules (implementing
     app.validation.base.ValidationRule);
  2. add one line to the tuple below.
Nothing in app.validation.engine ever needs to change.

Unlike REMEDIATION_RULES (which dispatches by issue_type), validation
dispatches by action -- so this module builds _RULES_BY_ACTION for O(1)
action → rule lookup via get_rule_for_action(). Every action in
REMEDIATION_ACTIONS has exactly one rule; the import-time assertions
below enforce this and all other structural invariants.

Import-time integrity checks (all enforced before any code can call
validate()):
  1. unique actions     -- no two rules share an action
  2. unique rule names  -- no two rules share a rule_name
  3. every REMEDIATION_ACTION covered -- one rule per action, no gaps
  4. every VALIDATION_RULE_NAME used  -- bijection with REMEDIATION_ACTIONS
  5. non-empty rule_version per rule  -- Adjustment 2 requirement
  6. len(VALIDATION_RULES) == 10      -- explicit count guard
"""
from app.models.enums import REMEDIATION_ACTIONS, VALIDATION_RULE_NAMES
from app.validation.base import ValidationRule
from app.validation.rules.booleans import ValidateNormalizeBooleanRule
from app.validation.rules.capitalization import ValidateStandardizeCapitalizationRule
from app.validation.rules.collapse import ValidateCollapseMultipleSpacesRule
from app.validation.rules.dates import ValidateNormalizeDateRule
from app.validation.rules.duplicates import (
    ValidateRemoveDuplicatePrimaryKeyRule,
    ValidateRemoveDuplicateRowRule,
)
from app.validation.rules.enum_values import ValidateNormalizeEnumValueRule
from app.validation.rules.numeric import ValidateNormalizeNumericRule
from app.validation.rules.phones import ValidateNormalizePhoneRule
from app.validation.rules.trim import ValidateTrimWhitespaceRule

VALIDATION_RULES: tuple[ValidationRule, ...] = (
    ValidateTrimWhitespaceRule(),
    ValidateCollapseMultipleSpacesRule(),
    ValidateStandardizeCapitalizationRule(),
    ValidateNormalizeBooleanRule(),
    ValidateNormalizeDateRule(),
    ValidateNormalizePhoneRule(),
    ValidateNormalizeNumericRule(),
    ValidateNormalizeEnumValueRule(),
    ValidateRemoveDuplicateRowRule(),
    ValidateRemoveDuplicatePrimaryKeyRule(),
)

# --- Integrity check 6: explicit count guard ---
assert len(VALIDATION_RULES) == 10, (
    f"VALIDATION_RULES must have exactly 10 rules, got {len(VALIDATION_RULES)}"
)

# --- Integrity check 1: unique actions ---
_actions = [rule.action for rule in VALIDATION_RULES]
assert len(_actions) == len(set(_actions)), (
    "two or more registered validation rules share an action -- "
    "each rule in VALIDATION_RULES must own a distinct action"
)

# --- Integrity check 2: unique rule names ---
_rule_names = [rule.rule_name for rule in VALIDATION_RULES]
assert len(_rule_names) == len(set(_rule_names)), (
    "two or more registered validation rules share a rule_name -- "
    "each rule in VALIDATION_RULES must have a distinct rule_name"
)

# --- Integrity check 3: every REMEDIATION_ACTION covered ---
_registered_actions = set(_actions)
for _action in REMEDIATION_ACTIONS:
    assert _action in _registered_actions, (
        f"REMEDIATION_ACTION {_action!r} has no corresponding validation rule "
        f"in VALIDATION_RULES -- add one or update the exclusion list"
    )

# --- Integrity check 4: every VALIDATION_RULE_NAME used ---
_registered_rule_names = set(_rule_names)
for _rule_name in VALIDATION_RULE_NAMES:
    assert _rule_name in _registered_rule_names, (
        f"VALIDATION_RULE_NAME {_rule_name!r} has no corresponding rule "
        f"in VALIDATION_RULES -- add one or correct the name"
    )

# --- Integrity check 5: non-empty rule_version per rule ---
for _rule in VALIDATION_RULES:
    assert _rule.rule_version, (
        f"rule {_rule.rule_name!r} has an empty rule_version -- "
        f"Adjustment 2 requires a non-empty version string per rule"
    )

# O(1) action → rule lookup used by the engine.
_RULES_BY_ACTION: dict[str, ValidationRule] = {
    rule.action: rule for rule in VALIDATION_RULES
}


def get_rule_for_action(action: str) -> ValidationRule | None:
    """Return the ValidationRule for the given REMEDIATION_ACTION, or
    None if the action is not in VALIDATION_RULES. None should never
    occur in a well-formed system (all 10 actions are covered by the
    assertions above), but the engine guards defensively anyway."""
    return _RULES_BY_ACTION.get(action)
