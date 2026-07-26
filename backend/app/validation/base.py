"""The ValidationRule interface every rule in app.validation.rules
implements. Deliberately the entire contract the engine
(app.validation.engine.validate) depends on -- it only ever knows "a
ValidationRule owns one action and a validate() method that returns a
ValidationRuleOutcome given one ValidationChangeInput and its resolved
config"; it never knows or cares how any individual rule decides its
outcome.

Parallel to app.remediation.base.RemediationRule with two additions:
  - rule_name: the VALIDATION_RULE_NAMES constant this rule is indexed
    under in app.validation.registry.VALIDATION_RULES.
  - rule_version: an independent per-rule version string (Adjustment 2
    from the Module 17 architecture), stored on every ValidationResult
    row so two results in the same ValidationRun can carry different
    rule versions if one rule was bumped independently.

validate() must be a pure function of its two arguments -- no I/O, no
wall-clock/locale/random dependence. Calling validate() twice with the
same change/column_config must return an identical ValidationRuleOutcome
every time; this is the engine's determinism/repeatability acceptance
criterion, verified by tests/test_validation_repeatability.py.

Adding a new rule never requires touching this file or engine.py, only
writing a new rule module and adding one line to
app.validation.registry.VALIDATION_RULES -- same "open for extension,
closed for modification" discipline app.remediation.base established."""
from __future__ import annotations

from typing import Protocol

from app.validation.types import (
    ValidationChangeInput,
    ValidationColumnConfig,
    ValidationRuleOutcome,
)


class ValidationRule(Protocol):
    """Every implementation must be a pure function of its arguments --
    no I/O, no wall-clock/locale/random dependence."""

    #: Always one of app.models.enums.VALIDATION_RULE_NAMES -- the
    #: validate_<action> constant this rule is registered under.
    rule_name: str

    #: Always one of app.models.enums.REMEDIATION_ACTIONS -- the action
    #: whose approved changes this rule validates.
    action: str

    #: Per-rule independent version string (Adjustment 2). Bumped when
    #: this specific rule's logic changes, never when other rules change.
    #: Always a non-empty string; enforced by registry import-time check.
    rule_version: str

    def validate(
        self,
        change: ValidationChangeInput,
        column_config: ValidationColumnConfig | None,
    ) -> ValidationRuleOutcome:
        """Return this rule's verdict for one approved RemediationChange.

        column_config is the resolved config for change.column_name
        (None if unconfigured or the change has no single column, e.g.
        remove_duplicate_row). A rule that does not need column_config
        simply ignores it, same convention as RemediationRule.propose().

        Must be deterministic: identical arguments must always produce
        an identical ValidationRuleOutcome."""
        ...
