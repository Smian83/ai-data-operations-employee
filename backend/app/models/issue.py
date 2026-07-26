"""
Issue: one structured finding produced by a Module 14 IssueDetectionRun.
Append-only, immutable child row -- direct structural sibling of
CleaningChange/StandardizationChange, capped per run at
settings.issue_detection_max_persisted_issues (see
IssueDetectionRun.persisted_issue_count / total_issues_found, which are
always the true, uncapped totals even when the rows here are capped).

Never written outside app.worker.handlers.issue_detection, and never
updated or deleted once written -- the engine is strictly read-only with
respect to source data, and this table is itself append-only with respect
to its own rows (no correction/edit path exists; a re-run creates a new
IssueDetectionRun and a fresh set of Issue rows instead).

Field names intentionally match the Module 14 requirements contract
verbatim (row_number, not this project's usual row_index -- see
CleaningChange -- since this is an externally-specified output shape, not
an internal audit-log convention). `dataset_id` from that same contract is
not a column on this table: it is surfaced at the API/schema layer via a
join to IssueDetectionRun.data_source_id, the same "child row references
only its immediate parent run, nothing denormalized beyond that" pattern
CleaningChange/StandardizationChange/MatchDecision/ExportRowExclusion/
ArtifactRetentionEvent all already use -- see app.schemas.issue_detection
(added in a later Module 14 phase).

Module 15 addition: UniqueConstraint(organization_id, id). Added
additively -- this table did not need to be a composite-FK *target* until
now. Module 15's RemediationChange.source_issue_id is a required composite
FK (organization_id, source_issue_id) -> issues(organization_id, id), and
this project's own established rule (learned in Module 6: add the
constraint at the same time a table is created, not after the first
FK-target failure surfaces on SQLite) requires it. Purely additive: does
not change any existing query, constraint, or row.
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
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import ISSUE_SEVERITIES, ISSUE_TYPES


class Issue(Base):
    __tablename__ = "issues"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "detection_run_id"],
            ["issue_detection_runs.organization_id", "issue_detection_runs.id"],
            name="fk_issues_org_detection_run",
            ondelete="CASCADE",
        ),
        # Module 15: required so RemediationChange can have a composite FK
        # (organization_id, source_issue_id) -> (organization_id, id), same
        # pattern as every other parent-of-child-audit-rows table in this
        # project.
        UniqueConstraint("organization_id", "id", name="uq_issues_org_id"),
        CheckConstraint(
            "issue_type IN (" + ", ".join(f"'{t}'" for t in ISSUE_TYPES) + ")",
            name="ck_issues_issue_type_valid",
        ),
        CheckConstraint(
            "severity IN (" + ", ".join(f"'{s}'" for s in ISSUE_SEVERITIES) + ")",
            name="ck_issues_severity_valid",
        ),
        CheckConstraint("row_number >= 0", name="ck_issues_row_number_nonnegative"),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_issues_confidence_range"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    detection_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)

    row_number: Mapped[int] = mapped_column(Integer(), nullable=False)
    # Nullable: duplicate_row is a whole-row finding, not a single-column
    # one -- every other issue_type sets this.
    column_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    issue_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    # Nullable: a missing_value finding (a structurally absent cell -- see
    # app.detection.rules) has no original value to record at all.
    original_value: Mapped[str | None] = mapped_column(Text(), nullable=True)
    # Optional per the Module 14 contract -- the engine never applies a
    # suggested_fix itself (STRICTLY READ ONLY); this is advisory text only,
    # for a future remediation module to act on or ignore.
    suggested_fix: Mapped[str | None] = mapped_column(Text(), nullable=True)
    confidence: Mapped[float] = mapped_column(Float(), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    detection_run: Mapped["IssueDetectionRun"] = relationship(back_populates="issues")
    # Module 15: normally empty/one-element -- an Issue is remediated at
    # most once per RemediationRun, and typically only ever considered by
    # one. A list (not uselist=False) since nothing prevents multiple
    # independent RemediationRuns from each considering the same Issue
    # (see docs/module-15-deterministic-cleaning-engine-design.md Risk R5).
    remediation_changes: Mapped[list["RemediationChange"]] = relationship(  # noqa: F821
        back_populates="source_issue"
    )

    def __repr__(self) -> str:
        return (
            f"Issue(detection_run={self.detection_run_id!r}, row={self.row_number!r}, "
            f"column={self.column_name!r}, issue_type={self.issue_type!r}, "
            f"severity={self.severity!r})"
        )
