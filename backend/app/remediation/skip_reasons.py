"""
The closed vocabulary of reasons a considered Issue produced no
RemediationChange. Every SkippedIssue.reason the engine ever produces is
exactly one of these constants -- never a free-text string built at the
call site -- mirroring app.detection.issue_types' "named constants, not
scattered literals" precedent. Not persisted as a database column
anywhere (RemediationRun only stores the count, issues_skipped_count), so
there is no CHECK constraint to keep this list in sync with the way
ISSUE_TYPES/REMEDIATION_ACTIONS are; the assertion below is this module's
own internal completeness guard instead.

Ten reasons, three families:
  - structural (2): the Issue itself can't be acted on at all, regardless
    of any configuration -- ISSUE_TYPE_NOT_REMEDIABLE,
    ROW_NOT_FOUND_IN_DATASET.
  - missing configuration (6): a required per-column/per-dataset gate
    (Module 15 decisions 6/8/9/10 -- "never guess") was not satisfied --
    CAPITALIZATION_TARGET_NOT_CONFIGURED, SOURCE_DATE_FORMAT_NOT_CONFIGURED,
    TARGET_DATE_FORMAT_NOT_CONFIGURED, DEFAULT_COUNTRY_NOT_CONFIGURED,
    ALLOWED_VALUES_NOT_CONFIGURED, DUPLICATE_REMOVAL_NOT_ENABLED.
  - no safe change (2): the gate WAS satisfied but the deterministic
    function/logic itself determined nothing should change --
    NO_DETERMINISTIC_CHANGE_AVAILABLE (value already conforms, or the
    input was too ambiguous to touch -- see app.standardization.rules'
    own (value, None) convention, which does not distinguish the two
    cases and neither does Module 15) -- and
    PERSISTED_CHANGE_LIMIT_REACHED (settings.remediation_max_persisted_
    changes' defensive ceiling was hit; see engine.py).
"""

# --- Structural ---
ISSUE_TYPE_NOT_REMEDIABLE = "issue_type_not_remediable"
ROW_NOT_FOUND_IN_DATASET = "row_not_found_in_dataset"

# --- Missing configuration (never guess) ---
CAPITALIZATION_TARGET_NOT_CONFIGURED = "capitalization_target_not_configured"
SOURCE_DATE_FORMAT_NOT_CONFIGURED = "source_date_format_not_configured"
TARGET_DATE_FORMAT_NOT_CONFIGURED = "target_date_format_not_configured"
DEFAULT_COUNTRY_NOT_CONFIGURED = "default_country_not_configured"
ALLOWED_VALUES_NOT_CONFIGURED = "allowed_values_not_configured"
DUPLICATE_REMOVAL_NOT_ENABLED = "duplicate_removal_not_enabled"

# --- No safe change ---
NO_DETERMINISTIC_CHANGE_AVAILABLE = "no_deterministic_change_available"
PERSISTED_CHANGE_LIMIT_REACHED = "persisted_change_limit_reached"

_ALL = (
    ISSUE_TYPE_NOT_REMEDIABLE,
    ROW_NOT_FOUND_IN_DATASET,
    CAPITALIZATION_TARGET_NOT_CONFIGURED,
    SOURCE_DATE_FORMAT_NOT_CONFIGURED,
    TARGET_DATE_FORMAT_NOT_CONFIGURED,
    DEFAULT_COUNTRY_NOT_CONFIGURED,
    ALLOWED_VALUES_NOT_CONFIGURED,
    DUPLICATE_REMOVAL_NOT_ENABLED,
    NO_DETERMINISTIC_CHANGE_AVAILABLE,
    PERSISTED_CHANGE_LIMIT_REACHED,
)
assert len(_ALL) == len(set(_ALL)) == 10, "skip reason constants must be unique"
