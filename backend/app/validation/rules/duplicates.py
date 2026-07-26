"""validate_remove_duplicate_row / validate_remove_duplicate_primary_key:
validates RemediationChange proposals for the "remove_duplicate_row" and
"remove_duplicate_primary_key" actions (handle DUPLICATE_ROW and
DUPLICATE_PRIMARY_KEY from Module 14).

Both duplicate-removal actions in Module 15 always produce
proposed_value=None when they propose a change (exclusion of the row,
not a replacement value -- see app.remediation.actions.duplicates).
These validation rules check only the structural invariant: a valid
proposal for either action must carry proposed_value=None.

No column_config, no standardization function, no config gate -- the
structural check is the entire validation. A proposed_value that is
not None indicates a corrupted or misclassified proposal."""
from __future__ import annotations

from app.validation.reasons import (
    DUPLICATE_REMOVAL_CORRECTLY_FORMED,
    DUPLICATE_REMOVAL_PROPOSED_VALUE_NOT_NONE,
)
from app.validation.types import (
    ValidationChangeInput,
    ValidationColumnConfig,
    ValidationRuleOutcome,
)

_REMOVE_DUPLICATE_ROW_RULE_NAME = "validate_remove_duplicate_row"
_REMOVE_DUPLICATE_ROW_ACTION = "remove_duplicate_row"

_REMOVE_DUPLICATE_PK_RULE_NAME = "validate_remove_duplicate_primary_key"
_REMOVE_DUPLICATE_PK_ACTION = "remove_duplicate_primary_key"

RULE_VERSION = "1.0"


def _validate_duplicate_removal(
    change: ValidationChangeInput,
    column_config: ValidationColumnConfig | None,
) -> ValidationRuleOutcome:
    if change.proposed_value is None:
        return ValidationRuleOutcome(
            outcome="passed", reason=DUPLICATE_REMOVAL_CORRECTLY_FORMED
        )
    return ValidationRuleOutcome(
        outcome="failed", reason=DUPLICATE_REMOVAL_PROPOSED_VALUE_NOT_NONE
    )


class ValidateRemoveDuplicateRowRule:
    rule_name = _REMOVE_DUPLICATE_ROW_RULE_NAME
    action = _REMOVE_DUPLICATE_ROW_ACTION
    rule_version = RULE_VERSION

    def validate(
        self,
        change: ValidationChangeInput,
        column_config: ValidationColumnConfig | None,
    ) -> ValidationRuleOutcome:
        return _validate_duplicate_removal(change, column_config)


class ValidateRemoveDuplicatePrimaryKeyRule:
    rule_name = _REMOVE_DUPLICATE_PK_RULE_NAME
    action = _REMOVE_DUPLICATE_PK_ACTION
    rule_version = RULE_VERSION

    def validate(
        self,
        change: ValidationChangeInput,
        column_config: ValidationColumnConfig | None,
    ) -> ValidationRuleOutcome:
        return _validate_duplicate_removal(change, column_config)
