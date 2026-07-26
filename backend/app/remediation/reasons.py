"""Fixed, deterministic reason text for every RemediationChangeProposal,
keyed by action -- same REASONS-dict-per-rule convention as
app.cleaning.rules.RULE_REASONS / app.detection's per-Finding severity
constants. Every action in app.remediation.actions looks its reason up
here rather than building one dynamically at the call site, so the
"deterministic reason" the Module 15 requirements contract calls for is
provably fixed per action, never data-dependent phrasing."""
from app.models.enums import REMEDIATION_ACTIONS

CHANGE_REASONS: dict[str, str] = {
    "trim_whitespace": (
        "Leading or trailing whitespace removed (Module 14 suggested_fix reused verbatim)."
    ),
    "collapse_multiple_spaces": (
        "Repeated internal whitespace collapsed to a single space "
        "(Module 14 suggested_fix reused verbatim)."
    ),
    "standardize_capitalization": (
        "Value recased to the column's configured capitalization target."
    ),
    "normalize_boolean": (
        "Value normalized to the canonical boolean form "
        "(app.standardization.rules.values.standardize_boolean)."
    ),
    "normalize_date": (
        "Value reparsed under the column's configured source date format and "
        "rendered into the column's configured target date format."
    ),
    "normalize_phone": (
        "Value reformatted to E.164 using the column's configured default country "
        "(app.standardization.rules.contact.standardize_phone)."
    ),
    "normalize_numeric": (
        "Value normalized to canonical numeric form "
        "(app.standardization.rules.values.standardize_numeric)."
    ),
    "normalize_enum_value": (
        "Value matched case-insensitively to exactly one configured allowed value."
    ),
    "remove_duplicate_row": (
        "Row proposed for exclusion as an exact duplicate of an earlier row "
        "(Module 14 duplicate_row finding)."
    ),
    "remove_duplicate_primary_key": (
        "Row proposed for exclusion due to a duplicate primary key value "
        "(Module 14 duplicate_primary_key finding)."
    ),
}
assert set(CHANGE_REASONS) == set(REMEDIATION_ACTIONS), (
    "app.remediation.reasons.CHANGE_REASONS has drifted from "
    "app.models.enums.REMEDIATION_ACTIONS"
)
