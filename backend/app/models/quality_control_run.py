"""
QualityControlRun: immutable summary produced for one Module 18 QUALITY_CTRL
TaskRun. Direct structural sibling of IssueDetectionRun (Module 14),
RemediationRun (Module 15), and ValidationRun (Module 17) -- one row per
TaskRun, no status column, no approval fields, never updated after creation.
The engine never modifies any source data, upstream row, or prior module's
output, so there is nothing for a human to approve, reject, or roll back.

Consumes the completed ValidationRun produced by Module 17 (via
source_task_run_id chaining -- same convention every prior chained module
uses). Produces a single authoritative release recommendation (PASS /
PASS_WITH_WARNINGS / FAIL) for the post-remediation working dataset.

Two UNIQUE constraints:
  uq_quality_control_runs_task_run_id   -- idempotency: one row per
    QUALITY_CTRL TaskRun execution (same as every prior run-summary table).
  uq_quality_control_runs_validation_run_id -- one authoritative release
    decision per ValidationRun (enforces "no competing decisions": a second
    QUALITY_CTRL TaskRun pointing at the same ValidationRun hits an
    IntegrityError that the handler catch-and-refetches). Module 19 queries:
    QualityControlRun WHERE validation_run_id = ? AND organization_id = ? to
    get the single authoritative PASS/FAIL decision before export.

overall_score is nullable (Float):
  - Non-null: at least one quality category was applicable and evaluated;
    value is in [0.0, 100.0].
  - NULL: no applicable categories -- every registered category was skipped.
    The release_recommendation is always 'FAIL' in this case (a dataset with
    no applicable evidence must never be released). See architecture
    Section 11 (missing-evidence release rules).

execution_snapshot (JSON) stores every input used to produce this decision:
ValidationRun ID, ValidationResult count + SHA-256 hash of ordered IDs,
threshold values, category weights, list of applicable categories, engine
version. See architecture Section 7 for the exact schema.

No processing_duration_ms column -- derived at the API layer from
TaskRun.started_at/finished_at, never stored. Same decision as
RemediationRun and ValidationRun. See
docs/module-18-quality-control-engine-design.md.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import QUALITY_RELEASE_RECOMMENDATIONS


class QualityControlRun(Base):
    __tablename__ = "quality_control_runs"
    __table_args__ = (
        # ---- Composite FK → task_runs (CASCADE) ----
        ForeignKeyConstraint(
            ["organization_id", "task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_quality_control_runs_org_task_run",
            ondelete="CASCADE",
        ),
        # ---- Composite FK → tasks (RESTRICT) ----
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_quality_control_runs_org_task",
            ondelete="RESTRICT",
        ),
        # ---- Composite FK → data_sources (RESTRICT) ----
        ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_quality_control_runs_org_data_source",
            ondelete="RESTRICT",
        ),
        # ---- Composite FK → validation_runs (RESTRICT) ----
        # RESTRICT: a QualityControlRun audit row must not silently disappear
        # alongside the ValidationRun it validated. Same ondelete=RESTRICT
        # reasoning as ValidationRun.fk_validation_runs_org_remediation_run.
        ForeignKeyConstraint(
            ["organization_id", "validation_run_id"],
            ["validation_runs.organization_id", "validation_runs.id"],
            name="fk_quality_control_runs_org_validation_run",
            ondelete="RESTRICT",
        ),
        # ---- Idempotency: one QC run per QUALITY_CTRL TaskRun ----
        UniqueConstraint("task_run_id", name="uq_quality_control_runs_task_run_id"),
        # ---- One authoritative release decision per ValidationRun ----
        # A second QUALITY_CTRL TaskRun pointing at the same ValidationRun
        # hits an IntegrityError that the handler catch-and-refetches,
        # returning the first run's decision. Module 19 reads the single
        # row identified by validation_run_id + organization_id.
        UniqueConstraint(
            "validation_run_id",
            name="uq_quality_control_runs_validation_run_id",
        ),
        # ---- Required so QualityFinding can use a composite FK target ----
        UniqueConstraint(
            "organization_id", "id", name="uq_quality_control_runs_org_id"
        ),
        # ---- overall_score: NULL when no categories apply; [0,100] otherwise ----
        CheckConstraint(
            "overall_score IS NULL OR "
            "(overall_score >= 0.0 AND overall_score <= 100.0)",
            name="ck_quality_control_runs_score_range",
        ),
        # ---- Non-negative finding counts ----
        CheckConstraint(
            "total_findings >= 0",
            name="ck_quality_control_runs_total_findings_nonneg",
        ),
        CheckConstraint(
            "blocking_count >= 0",
            name="ck_quality_control_runs_blocking_nonneg",
        ),
        CheckConstraint(
            "warning_count >= 0",
            name="ck_quality_control_runs_warning_nonneg",
        ),
        CheckConstraint(
            "info_count >= 0",
            name="ck_quality_control_runs_info_nonneg",
        ),
        # ---- Count reconciliation ----
        CheckConstraint(
            "blocking_count + warning_count + info_count = total_findings",
            name="ck_quality_control_runs_counts_reconcile",
        ),
        # ---- release_recommendation closed vocabulary ----
        CheckConstraint(
            "release_recommendation IN ("
            + ", ".join(f"'{v}'" for v in QUALITY_RELEASE_RECOMMENDATIONS)
            + ")",
            name="ck_quality_control_runs_recommendation_valid",
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
    task_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    data_source_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)

    # The specific ValidationRun whose results this QC run evaluated.
    # Primary input to the quality engine; also used by Module 19 to resolve
    # the single authoritative release decision for a given validation pass.
    validation_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), nullable=False, index=True
    )

    # Denormalized upstream IDs resolved at handler time -- stored here so
    # API responses and audit queries never need to re-traverse the TaskRun
    # chain. Same denormalization convention as ValidationRun.remediation_run_id.
    remediation_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), nullable=False, index=True
    )
    issue_detection_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), nullable=False, index=True
    )
    data_profile_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), nullable=False, index=True
    )

    quality_engine_version: Mapped[str] = mapped_column(String(20), nullable=False)

    # NULL when no categories are applicable (all registered categories were
    # skipped). Non-null when at least one category was evaluated; always in
    # [0.0, 100.0] when non-null. See ck_quality_control_runs_score_range.
    overall_score: Mapped[float | None] = mapped_column(Float(), nullable=True)

    # 'PASS' | 'PASS_WITH_WARNINGS' | 'FAIL' -- enforced by
    # ck_quality_control_runs_recommendation_valid. 'FAIL' is always set
    # when overall_score is NULL (no applicable categories).
    release_recommendation: Mapped[str] = mapped_column(String(30), nullable=False)

    # True total of QualityFinding rows the engine produced. The persisted
    # row count may be lower (bounded by settings.quality_max_persisted_findings).
    # Same bounded-but-never-silent pattern as total_issues_found on
    # IssueDetectionRun.
    total_findings: Mapped[int] = mapped_column(Integer(), nullable=False)
    blocking_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    warning_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    info_count: Mapped[int] = mapped_column(Integer(), nullable=False)

    # Per-category breakdown stored as JSON so the summary API endpoint
    # never needs to aggregate QualityFinding rows.
    # category_scores: {category: float} -- absent keys = skipped categories
    #   (not present as null; omission is the signal).
    # category_statuses: {category: "passed"|"warning"|"failed"|"skipped"}
    #   -- all 8 categories always present (skipped categories appear here,
    #   not as QualityFinding rows).
    # category_weights_used: {category: float} -- only applicable (non-skipped)
    #   categories present; records the effective weight used in scoring.
    category_scores: Mapped[dict] = mapped_column(JSON, nullable=False)
    category_statuses: Mapped[dict] = mapped_column(JSON, nullable=False)
    category_weights_used: Mapped[dict] = mapped_column(JSON, nullable=False)

    # Computed effective post-remediation statistics derived from DataProfile
    # baseline + approved+validated-passed RemediationChange deltas. See
    # architecture Section 3 for the exact JSON schema and computation rules.
    # Schema version is embedded in the JSON (stats_version key) so future
    # schema changes are forward-compatible without a column change.
    post_remediation_stats: Mapped[dict] = mapped_column(JSON, nullable=False)

    # Full deterministic snapshot of every input used to produce this decision.
    # See architecture Section 7 for the exact schema, stable key ordering,
    # and SHA-256 ID-hash approach for large ValidationResult ID sets.
    # An auditor can use this field alone to independently verify the decision.
    execution_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    findings: Mapped[list["QualityFinding"]] = relationship(  # noqa: F821
        back_populates="quality_control_run",
        order_by="QualityFinding.created_at",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return (
            f"QualityControlRun(id={self.id!r}, "
            f"task_run={self.task_run_id!r}, "
            f"overall_score={self.overall_score!r}, "
            f"recommendation={self.release_recommendation!r})"
        )
