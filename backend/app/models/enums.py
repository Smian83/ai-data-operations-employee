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
