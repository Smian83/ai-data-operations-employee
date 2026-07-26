"""normalize_date: handles INVALID_DATE. Deliberately NOT a reuse of
app.standardization.rules.temporal.standardize_date -- that function (like
Module 14's own invalid_date detector, app.detection.validators.
is_valid_iso_date) is strictly datetime.fromisoformat-based, so calling it
here would almost always no-op: anything Module 14 flagged as invalid
already failed that identical parser (Module 15 design doc Risk R1).

Instead: a single deterministic datetime.strptime(value, source_date_format)
parse using ONLY the column's explicitly configured source format -- never
a multi-format "try a few things" fallback, which is exactly the
MM/DD-vs-DD/MM guessing trap this project has consistently avoided
elsewhere (decision 10: "Date remediation must require an explicit
per-column input format where parsing is ambiguous. Never guess dates.").

Output rendering is symmetric: a single deterministic
parsed.strftime(target_date_format) using ONLY the column's explicitly
configured target format -- never inferred from source_date_format's own
shape. Both source_date_format AND target_date_format must be configured
before any proposal is made; either missing is its own distinct,
documented skip reason so a caller can tell which half of the
configuration is incomplete. Whether the output ends up date-only
("%Y-%m-%d") or includes a time component ("%Y-%m-%dT%H:%M:%S") is
entirely a function of what the organization put in target_date_format --
this module never appends a time-of-day that the configured target format
did not itself ask for, so a date-only target never gets a forced
00:00:00, and a datetime target only ever shows 00:00:00 when that is the
real (or genuinely absent-from-the-source) time value, exactly as
strftime would render it for any other datetime."""
from __future__ import annotations

from datetime import datetime

from app.detection.issue_types import INVALID_DATE
from app.remediation.reasons import CHANGE_REASONS
from app.remediation.skip_reasons import (
    NO_DETERMINISTIC_CHANGE_AVAILABLE,
    SOURCE_DATE_FORMAT_NOT_CONFIGURED,
    TARGET_DATE_FORMAT_NOT_CONFIGURED,
)
from app.remediation.types import (
    RemediationColumnConfig,
    RemediationDatasetConfigInput,
    RemediationIssueInput,
    RuleOutcome,
)

ACTION = "normalize_date"


def propose_normalize_date(
    issue: RemediationIssueInput, column_config: RemediationColumnConfig | None
) -> RuleOutcome:
    source_format = column_config.source_date_format if column_config else None
    if not source_format:
        return RuleOutcome(changed=False, skip_reason=SOURCE_DATE_FORMAT_NOT_CONFIGURED)

    target_format = column_config.target_date_format if column_config else None
    if not target_format:
        return RuleOutcome(changed=False, skip_reason=TARGET_DATE_FORMAT_NOT_CONFIGURED)

    value = issue.original_value
    if not value:
        return RuleOutcome(changed=False, skip_reason=NO_DETERMINISTIC_CHANGE_AVAILABLE)

    try:
        parsed = datetime.strptime(value, source_format)
    except ValueError:
        # Does not match the configured source format -- left untouched,
        # never a second-format guess.
        return RuleOutcome(changed=False, skip_reason=NO_DETERMINISTIC_CHANGE_AVAILABLE)

    # Rendered strictly according to the configured target format -- no
    # date-vs-datetime detection logic here at all; strftime already does
    # exactly what the org asked for, nothing more.
    canonical = parsed.strftime(target_format)
    if canonical == value:
        return RuleOutcome(changed=False, skip_reason=NO_DETERMINISTIC_CHANGE_AVAILABLE)
    return RuleOutcome(changed=True, proposed_value=canonical, reason=CHANGE_REASONS[ACTION])


class NormalizeDateRule:
    issue_types = (INVALID_DATE,)
    action = ACTION

    def propose(
        self,
        issue: RemediationIssueInput,
        column_config: RemediationColumnConfig | None,
        dataset_config: RemediationDatasetConfigInput,
    ) -> RuleOutcome:
        return propose_normalize_date(issue, column_config)
