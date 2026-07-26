"""Closed vocabulary of reason strings for ValidationResultItem.reason.
Every outcome the validation engine produces is exactly one constant
from this module -- never a free-text string built at the call site.

Mirrors app.remediation.skip_reasons' "named constants, not scattered
literals" discipline. Not persisted as a separate column with a CHECK
constraint (the reason is TEXT on ValidationResult, unbounded for
encryption compatibility per Adjustment 4). The assertion below is this
module's own completeness/uniqueness guard.

Three families:

  PASSED_*  : the change was successfully validated and proposed_value
              matches what the deterministic function produces today.

  FAILED_*  : the change was successfully validated but proposed_value
              does NOT match. The proposal is inconsistent with the
              current deterministic function output for this original
              value and configuration.

  SKIPPED_* : the change could not be validated. The engine could not
              re-run the deterministic function (missing required config,
              missing original value, unrecognised action, or the
              per-run result ceiling was hit). Skipped does NOT mean the
              proposal is wrong -- it means validation was inconclusive
              for this change.

Config-gate skip reasons use the same string values as
app.remediation.skip_reasons' equivalents so that a caller can compare
them without importing from both modules.
"""

# --- Passed ---

# The deterministic function, called with today's config, produces the
# same proposed_value that Module 15 stored on the RemediationChange.
PROPOSED_VALUE_MATCHES = "proposed_value_matches"

# The duplicate-removal action is structurally correct: proposed_value
# is None, which is the only valid value for these actions (they propose
# exclusion of a row, never a replacement value).
DUPLICATE_REMOVAL_CORRECTLY_FORMED = "duplicate_removal_correctly_formed"

# --- Failed ---

# The deterministic function produces a DIFFERENT proposed_value than
# what Module 15 stored. Either the function logic changed (rule_version
# bump), the config was updated, or the stored proposal was corrupted.
PROPOSED_VALUE_MISMATCH = "proposed_value_mismatch"

# The duplicate-removal action has a non-None proposed_value, which
# contradicts the rule contract (removal actions never produce a
# replacement value).
DUPLICATE_REMOVAL_PROPOSED_VALUE_NOT_NONE = "duplicate_removal_proposed_value_not_none"

# --- Skipped ---

# The change's action is not in the registry -- no rule can validate it.
# Should never occur in a well-formed system (the registry's own
# import-time checks guarantee every REMEDIATION_ACTION has a rule),
# but guarded here defensively.
ACTION_NOT_RECOGNISED = "action_not_recognised"

# original_value is None; the validation function cannot produce a
# meaningful re-computation without it.
ORIGINAL_VALUE_MISSING = "original_value_missing"

# Column-config gates below mirror app.remediation.skip_reasons:
# the same string values so callers can compare across both modules.

# The column has no configured capitalization_target; cannot re-run
# standardize_capitalization without knowing the target case.
CAPITALIZATION_TARGET_NOT_CONFIGURED = "capitalization_target_not_configured"

# The column has no configured source_date_format; cannot parse the
# original_value without knowing the expected input format.
SOURCE_DATE_FORMAT_NOT_CONFIGURED = "source_date_format_not_configured"

# The column has no configured target_date_format; cannot render the
# output without knowing the expected output format.
TARGET_DATE_FORMAT_NOT_CONFIGURED = "target_date_format_not_configured"

# The column has no configured default_country; cannot convert to E.164
# without knowing the country context.
DEFAULT_COUNTRY_NOT_CONFIGURED = "default_country_not_configured"

# The column has no configured allowed_values list; cannot perform
# case-insensitive matching without the allowed-value vocabulary.
ALLOWED_VALUES_NOT_CONFIGURED = "allowed_values_not_configured"

# The per-run result ceiling (settings.validation_max_persisted_results)
# was reached; this change and all subsequent ones are skipped rather
# than silently dropped, preserving the count-reconcile invariant.
RESULT_LIMIT_REACHED = "result_limit_reached"

_ALL = (
    PROPOSED_VALUE_MATCHES,
    DUPLICATE_REMOVAL_CORRECTLY_FORMED,
    PROPOSED_VALUE_MISMATCH,
    DUPLICATE_REMOVAL_PROPOSED_VALUE_NOT_NONE,
    ACTION_NOT_RECOGNISED,
    ORIGINAL_VALUE_MISSING,
    CAPITALIZATION_TARGET_NOT_CONFIGURED,
    SOURCE_DATE_FORMAT_NOT_CONFIGURED,
    TARGET_DATE_FORMAT_NOT_CONFIGURED,
    DEFAULT_COUNTRY_NOT_CONFIGURED,
    ALLOWED_VALUES_NOT_CONFIGURED,
    RESULT_LIMIT_REACHED,
)
assert len(_ALL) == len(set(_ALL)), "validation reason constants must be unique"
