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
