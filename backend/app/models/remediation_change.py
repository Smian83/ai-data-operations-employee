"""
RemediationChange: one proposed, deterministic correction produced by a
Module 15 RemediationRun. Append-only, immutable child row -- structural
sibling of CleaningChange/StandardizationChange, but named proposed_value
rather than cleaned_value deliberately: nothing here has been applied to
any dataset. This row IS the deliverable -- there is no later "apply"
step in this module (see
docs/module-15-deterministic-cleaning-engine-design.md Section 1/7).

source_issue_id is REQUIRED (never NULL): every RemediationChange traces
to exactly one Module 14 Issue that caused it. Module 15 never proposes a
change that did not originate from a specific, already-detected Issue --
no blanket per-column pass, no business-rule-driven change, no email-
casing pass with nothing to attach to (all deliberately deferred/excluded,
see the design doc Section 4/7). One Issue produces at most one
RemediationChange per RemediationRun.

Never written outside app.worker.handlers.remediation, and never updated
or deleted once written -- same append-only guarantee as CleaningChange.
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
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import REMEDIATION_ACTIONS


class RemediationChange(Base):
    __tablename__ = "remediation_changes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "remediation_run_id"],
            ["remediation_runs.organization_id", "remediation_runs.id"],
            name="fk_remediation_changes_org_remediation_run",
            ondelete="CASCADE",
        ),
        # Required (decision 6): every RemediationChange traces to exactly
        # one Module 14 Issue. RESTRICT (not CASCADE) -- an Issue is never
        # deleted independently of its own parent IssueDetectionRun today,
        # but if that ever changes, a RemediationChange audit row must
        # never silently disappear alongside it.
        ForeignKeyConstraint(
            ["organization_id", "source_issue_id"],
            ["issues.organization_id", "issues.id"],
            name="fk_remediation_changes_org_source_issue",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "action IN (" + ", ".join(f"'{a}'" for a in REMEDIATION_ACTIONS) + ")",
            name="ck_remediation_changes_action_valid",
        ),
        CheckConstraint("row_number >= 0", name="ck_remediation_changes_row_number_nonneg"),
        CheckConstraint(
            "confidence = 1.0", name="ck_remediation_changes_confidence_fixed"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    remediation_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    source_issue_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)

    row_number: Mapped[int] = mapped_column(Integer(), nullable=False)
    # Nullable: remove_duplicate_row/remove_duplicate_primary_key are
    # whole-row proposals, not single-column ones -- every other action
    # sets this.
    column_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    action: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    # Nullable: remove_duplicate_row has no single original value (mirrors
    # Issue.original_value's own nullability for that issue type).
    original_value: Mapped[str | None] = mapped_column(Text(), nullable=True)
    # Nullable: the two removal actions propose exclusion, not a
    # replacement value.
    proposed_value: Mapped[str | None] = mapped_column(Text(), nullable=True)
    reason: Mapped[str] = mapped_column(Text(), nullable=False)
    # Always 1.0 -- rule-based only (ck_remediation_changes_confidence_fixed
    # enforces this at the database layer, not just by convention). Stored
    # as a real column rather than hardcoded so the schema shape stays
    # consistent with CleaningChange/StandardizationChange.
    confidence: Mapped[float] = mapped_column(Float(), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # overlaps: same documented Module 8 MatchDecision/MatchGroup/MatchRun
    # pattern as RemediationRun.changes' own comment -- both relationships
    # below write RemediationChange.organization_id via a different (but
    # always identical-valued) composite FK.
    remediation_run: Mapped["RemediationRun"] = relationship(
        back_populates="changes", overlaps="remediation_changes"
    )
    source_issue: Mapped["Issue"] = relationship(  # noqa: F821
        back_populates="remediation_changes", overlaps="changes,remediation_run"
    )

    def __repr__(self) -> str:
        return (
            f"RemediationChange(remediation_run={self.remediation_run_id!r}, "
            f"row={self.row_number!r}, column={self.column_name!r}, "
            f"action={self.action!r})"
        )
