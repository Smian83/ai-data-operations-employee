"""
QualityFinding: one structured finding produced by a Module 18
QualityControlRun for a specific quality category/rule evaluation.
Append-only, immutable child row -- direct structural sibling of Issue
(Module 14), RemediationChange (Module 15), and ValidationResult (Module 17).
Never written outside app.worker.handlers.quality_control, and never updated
or deleted once written.

Three severity levels: 'info' | 'warning' | 'blocking'. 'blocking' forces
release_recommendation = 'FAIL' unconditionally; 'warning' may allow PASS_
WITH_WARNINGS depending on the configured thresholds; 'info' never affects
the recommendation directly.

Three outcome values: 'passed' | 'failed' | 'skipped'. A 'skipped' finding
means the rule's evaluation preconditions were not met (different from the
always-skipped categories: those emit no finding at all). A finding with
outcome='passed' and severity='info' is still persisted for audit
completeness when the engine emits it.

Three nullable provenance columns (source_issue_id, remediation_change_id,
validation_result_id): set only when the finding can be traced to a specific
upstream row. Never guessed or inferred. All carry RESTRICT ondelete so a
referenced upstream row cannot silently disappear.

reason is Text() (unbounded) for future encryption compatibility, same
convention as ValidationResult.reason and RemediationChangeDecision.comment.

Finding rows are bounded per run by settings.quality_max_persisted_findings.
QualityControlRun.total_findings is always the true total from the engine;
the persisted count may be lower. See
docs/module-18-quality-control-engine-design.md Section 4.3.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import (
    QUALITY_CATEGORIES,
    QUALITY_FINDING_OUTCOMES,
    QUALITY_FINDING_SEVERITIES,
)


class QualityFinding(Base):
    __tablename__ = "quality_findings"
    __table_args__ = (
        # ---- Composite FK → quality_control_runs (CASCADE) ----
        # Uses the UNIQUE(organization_id, id) on quality_control_runs as the
        # composite FK target -- same pattern every child-audit-rows table uses.
        ForeignKeyConstraint(
            ["organization_id", "quality_control_run_id"],
            ["quality_control_runs.organization_id", "quality_control_runs.id"],
            name="fk_quality_findings_org_qc_run",
            ondelete="CASCADE",
        ),
        # ---- Simple FK → issues.id (RESTRICT) ----
        # RESTRICT: a QualityFinding audit row must not silently disappear
        # alongside the Issue it references. Simple FK (not composite) because
        # issues has no UNIQUE(organization_id, id) -- same situation and
        # reasoning as fk_validation_results_remediation_change.
        ForeignKeyConstraint(
            ["source_issue_id"],
            ["issues.id"],
            name="fk_quality_findings_source_issue",
            ondelete="RESTRICT",
        ),
        # ---- Simple FK → remediation_changes.id (RESTRICT) ----
        ForeignKeyConstraint(
            ["remediation_change_id"],
            ["remediation_changes.id"],
            name="fk_quality_findings_remediation_change",
            ondelete="RESTRICT",
        ),
        # ---- Composite FK → validation_results (RESTRICT) ----
        # Uses the UNIQUE(organization_id, id) on validation_results as the
        # composite FK target -- same pattern as above.
        ForeignKeyConstraint(
            ["organization_id", "validation_result_id"],
            ["validation_results.organization_id", "validation_results.id"],
            name="fk_quality_findings_org_validation_result",
            ondelete="RESTRICT",
        ),
        # ---- Required so a future table can use a composite FK target ----
        UniqueConstraint("organization_id", "id", name="uq_quality_findings_org_id"),
        # ---- severity closed vocabulary ----
        CheckConstraint(
            "severity IN ("
            + ", ".join(f"'{v}'" for v in QUALITY_FINDING_SEVERITIES)
            + ")",
            name="ck_quality_findings_severity_valid",
        ),
        # ---- outcome closed vocabulary ----
        CheckConstraint(
            "outcome IN ("
            + ", ".join(f"'{v}'" for v in QUALITY_FINDING_OUTCOMES)
            + ")",
            name="ck_quality_findings_outcome_valid",
        ),
        # ---- category closed vocabulary ----
        CheckConstraint(
            "category IN ("
            + ", ".join(f"'{v}'" for v in QUALITY_CATEGORIES)
            + ")",
            name="ck_quality_findings_category_valid",
        ),
        # ---- non-negative affected row count ----
        CheckConstraint(
            "affected_row_count IS NULL OR affected_row_count >= 0",
            name="ck_quality_findings_affected_row_count_nonneg",
        ),
        # ---- Composite index: category + severity for API filters ----
        Index(
            "ix_quality_findings_org_category_severity",
            "organization_id",
            "category",
            "severity",
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
    quality_control_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), nullable=False, index=True
    )

    category: Mapped[str] = mapped_column(String(50), nullable=False)
    rule_name: Mapped[str] = mapped_column(String(100), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(20), nullable=False)

    # 'info' | 'warning' | 'blocking' -- enforced by
    # ck_quality_findings_severity_valid.
    severity: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    # 'passed' | 'failed' | 'skipped' -- enforced by
    # ck_quality_findings_outcome_valid.
    outcome: Mapped[str] = mapped_column(String(20), nullable=False, index=True)

    # Text() for encryption compatibility -- same rationale as
    # ValidationResult.reason and RemediationChangeDecision.comment.
    reason: Mapped[str] = mapped_column(Text(), nullable=False)

    # Optional: the number of rows affected by this finding. NULL when the
    # finding is not row-level (e.g., a dataset-level finding like EMPTY_DATASET
    # or NO_APPLICABLE_CATEGORIES). ck_quality_findings_affected_row_count_nonneg
    # prevents negative counts.
    affected_row_count: Mapped[int | None] = mapped_column(Integer(), nullable=True)

    # Optional: the specific column this finding relates to, for column-level
    # findings (e.g., completeness or validity for one column). NULL for
    # dataset-level findings.
    affected_column: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Provenance links -- all nullable. Only set when the finding can be
    # traced to a specific upstream row; never guessed. RESTRICT FKs prevent
    # the referenced rows from silently disappearing.
    source_issue_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    remediation_change_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), nullable=True
    )
    validation_result_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), nullable=True
    )

    quality_engine_version: Mapped[str] = mapped_column(String(20), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    quality_control_run: Mapped["QualityControlRun"] = relationship(  # noqa: F821
        back_populates="findings",
        foreign_keys=[organization_id, quality_control_run_id],
    )

    def __repr__(self) -> str:
        return (
            f"QualityFinding(id={self.id!r}, "
            f"category={self.category!r}, "
            f"rule={self.rule_name!r}, "
            f"severity={self.severity!r}, "
            f"outcome={self.outcome!r})"
        )
