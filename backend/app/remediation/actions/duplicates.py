"""remove_duplicate_row / remove_duplicate_primary_key: handle DUPLICATE_ROW
and DUPLICATE_PRIMARY_KEY respectively. Both propose EXCLUSION of the exact
row_number Module 14 already identified -- no re-detection, no
re-derivation of which columns form a primary key (a duplicate_primary_key
Issue's column_name already encodes the composite key verbatim, e.g.
"col_a+col_b" -- see app.detection.rules.duplicates). Both are opt-in,
gated on RemediationDatasetConfig's two independent flags -- absence of an
active config row for a data source means both flags are False and
neither action is ever proposed (the documented default-off behavior,
Module 15 design doc Section 3/9). proposed_value is always None for both:
these are exclusion proposals, not replacement values."""
from __future__ import annotations

from app.detection.issue_types import DUPLICATE_PRIMARY_KEY, DUPLICATE_ROW
from app.remediation.reasons import CHANGE_REASONS
from app.remediation.skip_reasons import DUPLICATE_REMOVAL_NOT_ENABLED
from app.remediation.types import (
    RemediationColumnConfig,
    RemediationDatasetConfigInput,
    RemediationIssueInput,
    RuleOutcome,
)

REMOVE_DUPLICATE_ROW_ACTION = "remove_duplicate_row"
REMOVE_DUPLICATE_PRIMARY_KEY_ACTION = "remove_duplicate_primary_key"


def propose_remove_duplicate_row(dataset_config: RemediationDatasetConfigInput) -> RuleOutcome:
    if not dataset_config.remove_duplicate_rows_enabled:
        return RuleOutcome(changed=False, skip_reason=DUPLICATE_REMOVAL_NOT_ENABLED)
    return RuleOutcome(
        changed=True, proposed_value=None, reason=CHANGE_REASONS[REMOVE_DUPLICATE_ROW_ACTION]
    )


def propose_remove_duplicate_primary_key(
    dataset_config: RemediationDatasetConfigInput,
) -> RuleOutcome:
    if not dataset_config.remove_duplicate_primary_keys_enabled:
        return RuleOutcome(changed=False, skip_reason=DUPLICATE_REMOVAL_NOT_ENABLED)
    return RuleOutcome(
        changed=True,
        proposed_value=None,
        reason=CHANGE_REASONS[REMOVE_DUPLICATE_PRIMARY_KEY_ACTION],
    )


class RemoveDuplicateRowRule:
    issue_types = (DUPLICATE_ROW,)
    action = REMOVE_DUPLICATE_ROW_ACTION

    def propose(
        self,
        issue: RemediationIssueInput,
        column_config: RemediationColumnConfig | None,
        dataset_config: RemediationDatasetConfigInput,
    ) -> RuleOutcome:
        return propose_remove_duplicate_row(dataset_config)


class RemoveDuplicatePrimaryKeyRule:
    issue_types = (DUPLICATE_PRIMARY_KEY,)
    action = REMOVE_DUPLICATE_PRIMARY_KEY_ACTION

    def propose(
        self,
        issue: RemediationIssueInput,
        column_config: RemediationColumnConfig | None,
        dataset_config: RemediationDatasetConfigInput,
    ) -> RuleOutcome:
        return propose_remove_duplicate_primary_key(dataset_config)
