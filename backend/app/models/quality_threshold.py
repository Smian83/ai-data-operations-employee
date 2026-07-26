"""
QualityThreshold: organization-configured release thresholds and category
weights for Module 18's quality scoring model. Structural sibling of
IssueDetectionColumnRule (Module 14), RemediationColumnRule (Module 15),
and RemediationDatasetConfig (Module 15) -- same tenant-scoped, optional
data-source override convention, and the same two-partial-unique-index
pattern (NULL != NULL under SQL uniqueness semantics).

Scoping rules (same precedence chain every prior config table uses):
  data_source_id IS NOT NULL → data-source-specific threshold; overrides the
    org-wide default for that specific data source.
  data_source_id IS NULL     → org-wide default; applies when no data-source-
    specific row exists.
  Neither exists             → handler uses built-in defaults (see
    architecture Section 11: missing QualityThreshold is not an error).

The two-partial-unique-index pattern:
  uq_quality_thresholds_org_scope    -- UNIQUE(organization_id) WHERE
    data_source_id IS NULL: at most one org-wide threshold per org.
  uq_quality_thresholds_org_ds_scope -- UNIQUE(organization_id, data_source_id)
    WHERE data_source_id IS NOT NULL: at most one per org+data_source pair.
These two partial indexes together provide the uniqueness guarantee that a
single UNIQUE(organization_id, data_source_id) constraint cannot provide due
to NULL != NULL.

All threshold columns have DB CHECK constraints enforcing range validity.
These same constraints are re-checked by the handler before calling the pure
engine (pre-engine configuration validation -- architecture Section 9 Step 3).
An invalid threshold row that passes the DB layer is a schema bug; the handler
check adds a second layer of defense that raises PermanentExecutionError
before any engine computation begins.

pass_score_threshold MUST be strictly greater than fail_score_threshold.
  Enforced by: ck_quality_thresholds_pass_gt_fail.
  Rationale: equal thresholds create an ambiguous band where a score matches
  neither PASS nor FAIL, defaulting to PASS_WITH_WARNINGS inconsistently.

category_weights (JSON, nullable): per-category weight overrides. If set,
every key must be in QUALITY_CATEGORIES and every value must be a positive
number. Keys not present use the engine's built-in defaults. NULL = use all
built-in defaults. Handler validates before engine call; invalid weights →
PermanentExecutionError.

No write API exists for this table in Module 18 -- rows are inserted directly
(same pre-API state as RemediationColumnRule and IssueDetectionColumnRule in
their own Phase 1). See docs/module-18-quality-control-engine-design.md.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class QualityThreshold(Base):
    __tablename__ = "quality_thresholds"
    __table_args__ = (
        # ---- Optional FK → data_sources (RESTRICT) ----
        # Only applied when data_source_id IS NOT NULL. When NULL this FK is
        # not referenced (org-wide row). Same conditional-FK pattern as
        # RemediationColumnRule and IssueDetectionColumnRule.
        ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_quality_thresholds_org_data_source",
            ondelete="RESTRICT",
        ),
        # ---- Two-partial-unique-index pattern ----
        # Org-wide: at most one active threshold per organization when
        # data_source_id IS NULL. PostgreSQL and SQLite both support
        # partial unique indexes; the WHERE clause differs by dialect.
        Index(
            "uq_quality_thresholds_org_scope",
            "organization_id",
            unique=True,
            postgresql_where=text("data_source_id IS NULL"),
            sqlite_where=text("data_source_id IS NULL"),
        ),
        # Data-source-specific: at most one active threshold per
        # organization + data_source pair when data_source_id IS NOT NULL.
        Index(
            "uq_quality_thresholds_org_ds_scope",
            "organization_id",
            "data_source_id",
            unique=True,
            postgresql_where=text("data_source_id IS NOT NULL"),
            sqlite_where=text("data_source_id IS NOT NULL"),
        ),
        # ---- Score threshold range constraints ----
        CheckConstraint(
            "fail_score_threshold >= 0.0 AND fail_score_threshold <= 100.0",
            name="ck_quality_thresholds_fail_score_range",
        ),
        CheckConstraint(
            "pass_score_threshold >= 0.0 AND pass_score_threshold <= 100.0",
            name="ck_quality_thresholds_pass_score_range",
        ),
        # Strict: pass must be STRICTLY greater than fail (equal is rejected).
        # A score exactly equal to both would match neither PASS nor FAIL,
        # defaulting to PASS_WITH_WARNINGS inconsistently.
        CheckConstraint(
            "pass_score_threshold > fail_score_threshold",
            name="ck_quality_thresholds_pass_gt_fail",
        ),
        # ---- Rate constraints [0.0, 1.0] ----
        CheckConstraint(
            "max_validation_failure_rate >= 0.0 AND "
            "max_validation_failure_rate <= 1.0",
            name="ck_quality_thresholds_failure_rate_range",
        ),
        CheckConstraint(
            "max_validation_skip_rate >= 0.0 AND "
            "max_validation_skip_rate <= 1.0",
            name="ck_quality_thresholds_skip_rate_range",
        ),
        # ---- Non-negative count constraints ----
        CheckConstraint(
            "max_high_severity_unresolved >= 0",
            name="ck_quality_thresholds_high_sev_nonneg",
        ),
        CheckConstraint(
            "max_critical_severity_unresolved >= 0",
            name="ck_quality_thresholds_crit_sev_nonneg",
        ),
        CheckConstraint(
            "max_warnings_for_clean_pass >= 0",
            name="ck_quality_thresholds_max_warnings_nonneg",
        ),
        # ---- Version non-negative ----
        CheckConstraint(
            "version >= 0",
            name="ck_quality_thresholds_version_nonneg",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # NULL = org-wide default. Non-null = data-source-specific override.
    # The two-partial-unique-indexes above enforce at-most-one per scope.
    data_source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)

    # Active flag -- not soft-delete (there are no per-finding history tables
    # here). When False, the handler treats this row as absent and falls
    # through to the built-in defaults. Allows an operator to temporarily
    # disable a custom threshold without deleting it.
    is_active: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)

    # Monotonically increasing version for audit/idempotency -- bumped on
    # every update. Starts at 0. Not a migration version; records the
    # configuration version captured in execution_snapshot.
    version: Mapped[int] = mapped_column(Integer(), nullable=False, default=0)

    # ---- Score thresholds ----
    # Scores in [fail_score_threshold, pass_score_threshold) → PASS_WITH_WARNINGS.
    # pass_score_threshold MUST be strictly > fail_score_threshold.
    fail_score_threshold: Mapped[float] = mapped_column(
        Float(), nullable=False, default=60.0
    )
    pass_score_threshold: Mapped[float] = mapped_column(
        Float(), nullable=False, default=85.0
    )

    # ---- Validation-outcome tolerances ----
    # Fraction in [0.0, 1.0]. 0.0 = no failed results tolerated (default).
    max_validation_failure_rate: Mapped[float] = mapped_column(
        Float(), nullable=False, default=0.0
    )
    # Fraction in [0.0, 1.0]. Validation coverage score degrades when skip
    # rate exceeds this threshold; may trigger a WARNING or FAIL finding.
    max_validation_skip_rate: Mapped[float] = mapped_column(
        Float(), nullable=False, default=0.5
    )

    # ---- Unresolved issue severity tolerances ----
    # Counts of unresolved HIGH/CRITICAL Issues still present after remediation.
    # 0 = any such issue forces FAIL (default -- conservative).
    max_high_severity_unresolved: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0
    )
    max_critical_severity_unresolved: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0
    )

    # ---- Warning tolerance ----
    # Maximum number of warning-severity findings permitted before the
    # recommendation is degraded from PASS to PASS_WITH_WARNINGS.
    # 0 = any warning prevents a clean PASS (default).
    max_warnings_for_clean_pass: Mapped[int] = mapped_column(
        Integer(), nullable=False, default=0
    )

    # ---- Per-category weight overrides (JSON, nullable) ----
    # Keys must be in QUALITY_CATEGORIES; values must be positive numbers.
    # Absent keys use the engine's built-in default weights. NULL = use all
    # built-in defaults (handler treats NULL identically to an empty dict).
    # The handler validates this field before calling the pure engine:
    # unknown keys or non-positive values → PermanentExecutionError.
    category_weights: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now(), nullable=True
    )

    def __repr__(self) -> str:
        scope = (
            f"data_source={self.data_source_id!r}"
            if self.data_source_id
            else "org_wide"
        )
        return (
            f"QualityThreshold(id={self.id!r}, "
            f"org={self.organization_id!r}, "
            f"{scope}, "
            f"pass={self.pass_score_threshold!r}, "
            f"fail={self.fail_score_threshold!r})"
        )
