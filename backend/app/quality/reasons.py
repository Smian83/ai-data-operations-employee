"""Closed vocabulary of reason strings for QualityFinding rows.

Every reason constant here is used verbatim as QualityFinding.reason.
No finding ever uses a dynamically-constructed string — same discipline
as app.validation.reasons and app.remediation.skip_reasons.

Convention:
  - SCREAMING_SNAKE_CASE for the constant name.
  - The constant value IS the constant name (no separate human-readable
    string). The API layer can translate these for display if needed.

Import-time uniqueness assertion guarantees no two constants share a value,
which prevents silent aliasing when findings are filtered by reason.
"""

# ── Completeness ────────────────────────────────────────────────────────────

# Zero rows — nothing to evaluate; always BLOCKING.
EMPTY_DATASET = "EMPTY_DATASET"

# Missing-value rate ≥ fail threshold (score < 60); always BLOCKING.
CRITICAL_MISSING_RATE = "CRITICAL_MISSING_RATE"

# Missing-value rate in warning band (60 ≤ score < 85); always WARNING.
HIGH_MISSING_RATE = "HIGH_MISSING_RATE"

# Missing values present but rate is low (score ≥ 85); always INFO.
MISSING_VALUES_PRESENT = "MISSING_VALUES_PRESENT"

# ── Uniqueness ───────────────────────────────────────────────────────────────

# Duplicate-row rate ≥ fail threshold (score < 60); always BLOCKING.
CRITICAL_DUPLICATE_RATE = "CRITICAL_DUPLICATE_RATE"

# Duplicate-row rate in warning band (60 ≤ score < 85); always WARNING.
HIGH_DUPLICATE_RATE = "HIGH_DUPLICATE_RATE"

# Duplicates present but rate is low (score ≥ 85); always INFO.
DUPLICATES_PRESENT = "DUPLICATES_PRESENT"

# ── Validity ─────────────────────────────────────────────────────────────────

# Validation pass rate < 60% (score < 60); always BLOCKING.
CRITICAL_VALIDATION_FAILURE_RATE = "CRITICAL_VALIDATION_FAILURE_RATE"

# Validation pass rate in warning band (60 ≤ score < 85); always WARNING.
HIGH_VALIDATION_FAILURE_RATE = "HIGH_VALIDATION_FAILURE_RATE"

# Some failures but pass rate ≥ 85; always INFO.
VALIDATION_FAILURES_PRESENT = "VALIDATION_FAILURES_PRESENT"

# ── Consistency ──────────────────────────────────────────────────────────────

# A column had both passed and failed results for the same normalization rule,
# indicating inconsistent application of that rule across the column's values.
INCONSISTENT_NORMALIZATION = "INCONSISTENT_NORMALIZATION"

# ── Unresolved Risk ───────────────────────────────────────────────────────────

# One or more CRITICAL-severity Issues remain unaddressed after remediation.
# Severity: BLOCKING (any unresolved critical issue cannot be released).
UNRESOLVED_CRITICAL_ISSUES = "UNRESOLVED_CRITICAL_ISSUES"

# One or more HIGH-severity Issues remain unaddressed after remediation.
# Severity: WARNING (by default the release threshold also blocks this,
# but the finding itself is WARNING — the FAIL condition is checked at the
# release-decision level independently per Section 10, conditions 5 and 6).
UNRESOLVED_HIGH_ISSUES = "UNRESOLVED_HIGH_ISSUES"

# ── Validation Coverage ───────────────────────────────────────────────────────

# Skip rate exceeds max_validation_skip_rate threshold; always BLOCKING.
# Forces release_recommendation = FAIL via blocking_count > 0.
HIGH_SKIP_RATE = "HIGH_SKIP_RATE"

# Some results were skipped but skip rate is within threshold; always WARNING.
RESULTS_SKIPPED = "RESULTS_SKIPPED"

# ── Meta (engine-level, not tied to any single category) ─────────────────────

# Emitted when every registered category is skipped (no evidence available).
# Forces overall_score = NULL and release_recommendation = FAIL.
# category on the QualityFinding row is "quality_control" (a meta-category).
NO_APPLICABLE_CATEGORIES = "NO_APPLICABLE_CATEGORIES"

# ── Import-time uniqueness assertion ─────────────────────────────────────────

_ALL_REASONS = (
    EMPTY_DATASET,
    CRITICAL_MISSING_RATE,
    HIGH_MISSING_RATE,
    MISSING_VALUES_PRESENT,
    CRITICAL_DUPLICATE_RATE,
    HIGH_DUPLICATE_RATE,
    DUPLICATES_PRESENT,
    CRITICAL_VALIDATION_FAILURE_RATE,
    HIGH_VALIDATION_FAILURE_RATE,
    VALIDATION_FAILURES_PRESENT,
    INCONSISTENT_NORMALIZATION,
    UNRESOLVED_CRITICAL_ISSUES,
    UNRESOLVED_HIGH_ISSUES,
    HIGH_SKIP_RATE,
    RESULTS_SKIPPED,
    NO_APPLICABLE_CATEGORIES,
)

assert len(_ALL_REASONS) == len(set(_ALL_REASONS)), (
    "app.quality.reasons: duplicate reason value detected — each reason "
    "constant must be unique (no two constants may share the same string value)"
)
