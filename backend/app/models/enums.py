"""
Shared enums used by both the SQLAlchemy models (as native PostgreSQL enum
types) and the Pydantic schemas (as request/response validation). Defining
these once and importing them in both places is what makes "enforced at the
Pydantic layer AND the PostgreSQL layer" a single source of truth rather than
two lists that can drift apart.
"""
import enum


class SourceType(str, enum.Enum):
    POSTGRES = "postgres"
    MYSQL = "mysql"
    REST_API = "rest_api"
    CSV_UPLOAD = "csv_upload"
    S3 = "s3"
    OTHER = "other"


class TaskType(str, enum.Enum):
    SYNC = "sync"
    TRANSFORM = "transform"
    EXPORT = "export"
    OTHER = "other"
    # Module 7: a new value, not a reuse of an existing one -- TRANSFORM
    # already means "cleaning" (Module 6), EXPORT and OTHER are already
    # reserved/generic per the handler registry's own docstring. See
    # docs/module-7-data-standardization-engine-design.md Section 2.
    STANDARDIZE = "standardize"
    # Module 8: another new value, same reasoning -- none of SYNC/
    # TRANSFORM/STANDARDIZE/EXPORT/OTHER can be reused without overloading
    # an existing meaning. See
    # docs/module-8-data-matching-deduplication-design.md Section 2.
    MATCH = "match"
    # Module 14: another new value, same reasoning as STANDARDIZE/MATCH --
    # OTHER is deliberately generic/reserved-for-nothing-in-particular (see
    # the handler registry's own docstring) and every other existing value
    # already means something specific. DETECT is read-only by construction
    # (see app.worker.handlers.issue_detection): it never writes an output
    # file and has no approval state machine, closer in shape to SYNC's
    # CsvProfilingHandler than to TRANSFORM/STANDARDIZE/EXPORT.
    DETECT = "detect"
    # Module 15: another new value, same reasoning as STANDARDIZE/MATCH/
    # DETECT -- every existing value already means something specific.
    # REMEDIATE consumes Module 14's Issue rows and proposes deterministic
    # fixes; like DETECT it never writes an output file and has no
    # approval state machine (see app.models.remediation_run) -- it
    # persists proposals only, never a materialized cleaned dataset. See
    # docs/module-15-deterministic-cleaning-engine-design.md.
    REMEDIATE = "remediate"
    # Module 17: another new value, same reasoning as DETECT/REMEDIATE --
    # every existing value already means something specific. VALIDATE
    # consumes the approved RemediationChange proposals from Module 16 and
    # verifies each proposed value satisfies the intended remediation rule;
    # like DETECT and REMEDIATE it never writes an output file, never
    # modifies source data or upstream rows, and has no approval state
    # machine -- it persists ValidationResult rows only. See
    # docs/module-17-validation-engine-design.md.
    VALIDATE = "validate"
    # Module 18: another new value, same reasoning as DETECT/REMEDIATE/
    # VALIDATE -- every existing value already means something specific.
    # QUALITY_CTRL consumes the completed ValidationRun produced by Module 17
    # and applies a configurable scoring model across 8 quality categories to
    # produce a single authoritative release recommendation (PASS /
    # PASS_WITH_WARNINGS / FAIL) for the post-remediation dataset. Like
    # DETECT, REMEDIATE, and VALIDATE it never writes an output file, never
    # modifies source data or upstream rows, and has no approval state
    # machine -- it persists QualityControlRun + QualityFinding rows only.
    # Phase 1: registered on NoOpHandler until Phase 3's real handler lands
    # (same placeholder pattern DETECT/REMEDIATE/VALIDATE each passed through).
    # See docs/module-18-quality-control-engine-design.md.
    QUALITY_CTRL = "quality_ctrl"
    # Module 19: another new value, same reasoning as DETECT/REMEDIATE/
    # VALIDATE/QUALITY_CTRL -- every existing value already means something
    # specific. CLEAN_EXPORT consumes the completed QualityControlRun produced
    # by Module 18 and materializes an immutable, verified, downloadable
    # artifact (CSV or XLSX) from the approved Module 9 ExportRun. Unlike
    # prior "read-only" modules (DETECT/REMEDIATE/VALIDATE/QUALITY_CTRL),
    # CLEAN_EXPORT writes an output file -- the final, approved clean export.
    # It never modifies source data, never overwrites the Module 9 ExportRun
    # artifact, and only proceeds when the QualityControlRun recommendation is
    # PASS or PASS_WITH_WARNINGS.
    CLEAN_EXPORT = "clean_export"
    # APPLY_REMEDIATIONS: a new value bridging Module 16 (approval decisions)
    # and Module 17 (validation). Reads the approved Module 9 ExportRun
    # artifact, applies only the approved RemediationChange proposals from
    # Module 15 (filtered through Module 16 decisions), and materializes an
    # immutable remediated CSV to csv_remediated_root. Unlike REMEDIATE
    # (which only proposes changes) and VALIDATE (which only reads DB rows),
    # APPLY_REMEDIATIONS is the first module to actually materialize a
    # post-remediation dataset on disk. It never modifies the ExportRun
    # artifact or any upstream row.
    APPLY_REMEDIATIONS = "apply_remediations"


class TaskRunStatus(str, enum.Enum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"


# Module 7: plain tuples, not native Postgres enum types or enum.Enum
# classes -- same "small, internal, worker/config-owned value set -> plain
# string" precedent TaskRunEvent.event_type and CleaningRun.status already
# set (see models/cleaning_run.py and PROJECT_CONTEXT.md's Coding
# Standards). Homed here rather than in a single owning model file because,
# unlike CLEANING_RUN_STATUSES (used by exactly one model), both tuples
# below are shared by multiple Module 7 models and by app/standardization/
# and the API schema layer -- this is already the project's shared home
# for exactly that kind of cross-cutting classification constant.
STANDARDIZATION_RUN_STATUSES = ("pending_review", "approved", "rejected", "rolled_back")

# The 14 classifiable field types (Section 6 of the Module 7 design doc).
# Common abbreviations, letter casing, and whitespace normalization are
# deliberately NOT included -- they are mechanisms/passes applied within
# the rules below, not field types a column is ever classified as.
STANDARDIZATION_FIELD_TYPES = (
    "person_name",
    "company_name",
    "email",
    "phone",
    "postal_address",
    "city",
    "state_province",
    "country",
    "postal_code",
    "date",
    "time",
    "boolean",
    "numeric",
    "currency",
)


# Module 8: same "small, internal, worker/config-owned value set -> plain
# string" precedent as STANDARDIZATION_RUN_STATUSES above, applied to the
# matching/deduplication engine's own run-approval state machine and
# per-decision/per-field small closed sets. See
# docs/module-8-data-matching-deduplication-design.md Section 3.
MATCH_RUN_STATUSES = ("pending_review", "approved", "rejected", "rolled_back")

# comparison_type values a MatchRuleField may declare (Section 6/7 of the
# Module 8 design doc). No fuzzy/phonetic/approximate options exist --
# this fixed, closed set IS the "no AI/ML/approximate matching" boundary,
# enforced here (Pydantic layer) and via a matching CHECK constraint
# (database layer).
MATCH_RULE_COMPARISON_TYPES = ("exact", "normalized_exact")

# decision values a MatchDecision may record. "not_duplicate" is
# deliberately absent -- a pair scoring below the review threshold is
# never persisted at all (Section 6/13), so there is no third value here.
MATCH_DECISION_TYPES = ("duplicate", "ambiguous")


# Module 9: same "small, internal, worker/config-owned value set -> plain
# string" precedent as STANDARDIZATION_RUN_STATUSES/MATCH_RUN_STATUSES
# above, applied to the export engine's own run-approval state machine.
# See docs/module-9-data-export-engine-design.md Section 10. Note: unlike
# every module from 7 onward, Module 9 required NO new TaskType value --
# TaskType.EXPORT already existed (see the class above), so there is no
# accompanying comment there.
EXPORT_RUN_STATUSES = ("pending_review", "approved", "rejected", "rolled_back")


# Module 14: same "small, internal, worker/config-owned value set -> plain
# string" precedent as every closed vocabulary above, applied to the issue
# detection engine's own findings. issue_detection_runs/issues have no
# approval state machine at all (see app.models.issue_detection_run) -- the
# engine is strictly read-only, so there is no *_RUN_STATUSES tuple here.
#
# 17 of these 18 values are produced by a real detection rule in Module 14
# (app.detection). 'broken_fk_reference' is the one exception: the
# DetectionRule interface and its issue_type are wired in now so a future
# module can populate it without another migration, but no rule in this
# module ever emits it -- this project has no relationship-metadata
# concept yet for CSV data sources to check foreign keys against. It is
# explicitly NOT counted among Module 14's completed detectors. See
# app.detection.rules.broken_fk_reference's own docstring.
ISSUE_TYPES = (
    "missing_value",
    "empty_string",
    "null_value",
    "duplicate_row",
    "duplicate_primary_key",
    "invalid_email",
    "invalid_phone",
    "invalid_date",
    "invalid_numeric",
    "required_field_violation",
    "leading_whitespace",
    "trailing_whitespace",
    "multiple_internal_spaces",
    "inconsistent_capitalization",
    "boolean_inconsistency",
    "invalid_enum_value",
    "outlier",
    "broken_fk_reference",  # deferred -- see docstring above
)

# Deliberately uppercase (unlike every other closed vocabulary in this
# file) -- these are the literal severity labels specified for Module 14,
# matching the conventional logging-level spelling (INFO/WARNING/ERROR
# etc.) rather than this project's usual lowercase internal-state-machine
# convention (pending_review, approved, ...). Ordered least to most severe;
# nothing in the code depends on that ordering today, but it is kept
# meaningful for future sorting/display.
ISSUE_SEVERITIES = ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")

# The column-level "expected type or behavior" vocabulary an organization
# can explicitly assign via IssueDetectionColumnRule.expected_type (see
# app.models.issue_detection_column_rule). Deliberately a small subset of
# STANDARDIZATION_FIELD_TYPES above, not the full 14 -- Module 14 only
# validates the types it has a deterministic, reusable validator for (see
# app.detection.rules, which imports app.standardization's existing
# email/phone/date/numeric classifiers rather than rebuilding them).
# Per the approved Module 14 corrections: a column is only checked against
# one of these when a rule explicitly says so -- never inferred from the
# column's name or its data.
ISSUE_DETECTION_EXPECTED_TYPES = ("email", "phone", "date", "numeric", "boolean")


# Module 15: same "small, internal, worker/config-owned value set -> plain
# string" precedent as every closed vocabulary above, applied to
# RemediationChange.action. remediation_runs has no approval state machine
# at all (see app.models.remediation_run) -- like Module 14, this engine
# only ever persists proposals, so there is no *_RUN_STATUSES tuple here.
# See docs/module-15-deterministic-cleaning-engine-design.md Section 4 for
# the full issue_type -> action mapping and which app.cleaning/
# app.standardization function (if any) each action reuses.
# Module 16: same "small, internal, closed vocabulary -> plain string"
# precedent as every prior closed-vocabulary tuple in this file. Two
# values only: "approved" and "rejected". No "pending" value -- pending
# is represented by the ABSENCE of a RemediationChangeDecision row for a
# given change, not by a third enum value. Cross-checked at model-import
# time against ck_remediation_change_decisions_valid's CHECK constraint.
REMEDIATION_CHANGE_DECISION_VALUES = ("approved", "rejected")

REMEDIATION_ACTIONS = (
    "trim_whitespace",
    "collapse_multiple_spaces",
    "standardize_capitalization",
    "normalize_boolean",
    "normalize_date",
    "normalize_phone",
    "normalize_numeric",
    "normalize_enum_value",
    "remove_duplicate_row",
    "remove_duplicate_primary_key",
)


# Module 17: same "small, internal, worker/config-owned value set -> plain
# string" precedent as every closed vocabulary above. Three families:
#   VALIDATION_OUTCOMES     — the three possible result outcomes.
#   VALIDATION_RULE_NAMES   — the named validation rule that evaluated a
#                             change; one entry per REMEDIATION_ACTION,
#                             following the validate_<action> naming
#                             convention. Cross-checked at import time
#                             against VALIDATION_RULES in
#                             app.validation.registry (Phase 2).
#
# No *_RUN_STATUSES tuple: ValidationRun has no approval state machine --
# the engine never modifies anything, so there is nothing for a human to
# approve, reject, or roll back (same reasoning as IssueDetectionRun and
# RemediationRun). See docs/module-17-validation-engine-design.md Section 1.

VALIDATION_OUTCOMES = ("passed", "failed", "skipped")

VALIDATION_RULE_NAMES = (
    "validate_trim_whitespace",
    "validate_collapse_multiple_spaces",
    "validate_standardize_capitalization",
    "validate_normalize_boolean",
    "validate_normalize_date",
    "validate_normalize_phone",
    "validate_normalize_numeric",
    "validate_normalize_enum_value",
    "validate_remove_duplicate_row",
    "validate_remove_duplicate_primary_key",
)

assert len(VALIDATION_RULE_NAMES) == len(REMEDIATION_ACTIONS) == 10, (
    "VALIDATION_RULE_NAMES must have exactly one entry per REMEDIATION_ACTION"
)


# Module 18: same "small, internal, worker/config-owned value set -> plain
# string" precedent as every closed vocabulary above, applied to the quality
# control engine's own categories, findings, and release decisions.
# quality_control_runs/quality_findings have no approval state machine --
# the engine is strictly read-only (same reasoning as IssueDetectionRun,
# RemediationRun, and ValidationRun), so there is no *_RUN_STATUSES tuple.
# See docs/module-18-quality-control-engine-design.md.
#
# Eight named quality categories -- two are always-skipped V1 placeholders
# (referential_integrity: pending FK metadata; business_rule_compliance:
# deferred to Module 21). The rest are active in V1. Ordered consistently
# with Section 6 of the architecture document.
QUALITY_CATEGORIES = (
    "completeness",
    "uniqueness",
    "validity",
    "consistency",
    "referential_integrity",        # always skipped in V1 -- no finding emitted
    "business_rule_compliance",     # deferred to Module 21 -- no finding emitted
    "unresolved_risk",
    "validation_coverage",
)

# lowercase (info/warning/blocking) to match this project's internal-state-
# machine convention. 'blocking' is deliberately absent from ISSUE_SEVERITIES
# (that vocabulary uses INFO/LOW/MEDIUM/HIGH/CRITICAL) -- quality findings
# use their own closed set. Ordered least to most severe.
QUALITY_FINDING_SEVERITIES = ("info", "warning", "blocking")

# Three possible finding outcomes: passed, failed, skipped. Same closed set
# as VALIDATION_OUTCOMES -- deliberate parallel naming. A skipped finding
# means the rule's preconditions were not met (never a pass, never a fail).
QUALITY_FINDING_OUTCOMES = ("passed", "failed", "skipped")

# Three possible release recommendations. All uppercase to distinguish from
# the lowercase internal-state-machine vocabulary. PASS_WITH_WARNINGS is the
# middle ground: overall_score in [fail_threshold, pass_threshold) or any
# non-blocking warning findings. FAIL is both the definitive rejection AND
# the default for the no-applicable-categories case (overall_score = NULL).
QUALITY_RELEASE_RECOMMENDATIONS = ("PASS", "PASS_WITH_WARNINGS", "FAIL")

# Category-level statuses stored in QualityControlRun.category_statuses JSON.
# 'skipped' is a category-level concept only (always-skipped categories produce
# no QualityFinding -- the status here is sufficient audit evidence).
QUALITY_CATEGORY_STATUSES = ("passed", "warning", "failed", "skipped")

# Import-time cross-checks (same pattern as VALIDATION_RULE_NAMES assertion).
assert len(QUALITY_CATEGORIES) == 8, (
    "QUALITY_CATEGORIES must have exactly 8 entries (Section 6 of the "
    "Module 18 architecture document)"
)
assert len(set(QUALITY_CATEGORIES)) == 8, "QUALITY_CATEGORIES must be unique"
assert len(QUALITY_FINDING_SEVERITIES) == 3, "QUALITY_FINDING_SEVERITIES must have 3 entries"
assert len(QUALITY_FINDING_OUTCOMES) == 3, "QUALITY_FINDING_OUTCOMES must have 3 entries"
assert len(QUALITY_RELEASE_RECOMMENDATIONS) == 3, (
    "QUALITY_RELEASE_RECOMMENDATIONS must have 3 entries"
)
assert len(QUALITY_CATEGORY_STATUSES) == 4, "QUALITY_CATEGORY_STATUSES must have 4 entries"


# Module 19: same "small, internal, worker/config-owned value set -> plain
# string" precedent as every closed vocabulary above, applied to the clean
# export engine's own per-export lifecycle states. See
# docs/module-19-clean-export-engine-design.md.
#
# Lifecycle:
#   pending    → export created, not yet processed (async path only)
#   processing → export actively running
#   completed  → artifact written, checksum stored, ready for download
#   failed     → export attempt failed (see failure_reason column)
#   blocked    → dataset ineligible: approval/validation/QC check failed
#   expired    → artifact deleted by retention policy; metadata remains
CLEAN_EXPORT_STATUSES = (
    "pending",
    "processing",
    "completed",
    "failed",
    "blocked",
    "expired",
)

# Two supported output formats for clean exports (CSV and XLSX).
CLEAN_EXPORT_FORMATS = ("csv", "xlsx")

assert len(CLEAN_EXPORT_STATUSES) == 6, "CLEAN_EXPORT_STATUSES must have 6 entries"
assert len(CLEAN_EXPORT_FORMATS) == 2, "CLEAN_EXPORT_FORMATS must have 2 entries"
