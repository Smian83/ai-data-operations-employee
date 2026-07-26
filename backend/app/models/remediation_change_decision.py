"""
RemediationChangeDecision: one row per decision event for a Module 15
RemediationChange proposal. Append-only — rows are never edited or
deleted. The effective state of a change is always the row with the
latest decision_timestamp for that change_id; earlier rows are the
immutable history.

Module 16 (Approval Queue) creates one row when a human approves or
rejects a RemediationChange proposal. Module 16's API enforces one
decision per change at the application layer (409 on re-decision), but
the schema does not enforce this via a unique constraint — that
deliberate omission keeps the future admin-override path open without
requiring a migration: an override is simply a new row with a later
decision_timestamp, and the "latest row wins" convention makes it the
new effective state without erasing the original decision from history.

Three families of nullable columns:

1. Reviewer snapshot (reviewer_name, reviewer_role): populated from the
   authenticated user's profile at decision time. Preserved independently
   of the reviewer_id FK — if the user's name or role changes after the
   fact, or the user row is soft-deleted, the snapshot still records
   exactly who decided and in what capacity.

2. reviewer_id FK (SET NULL on delete): referential integrity while the
   user exists. Goes NULL if the user row is ever deleted — same
   convention as CleaningRun.approved_by, StandardizationRun.approved_by,
   and every other reviewer/actor FK in this project.

3. Apply fields (applied_at, applied_by): always NULL in Module 16.
   Reserved for the future Apply/Export module, which will populate these
   either on a new append row or on an existing approved row — that
   design decision is deferred. Either approach is accommodated because
   the schema carries the columns regardless.

See docs/module-16-approval-queue-design.md for the full design
rationale (Section 3) and state-transition rules (Section 4).
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import REMEDIATION_CHANGE_DECISION_VALUES


class RemediationChangeDecision(Base):
    __tablename__ = "remediation_change_decisions"
    __table_args__ = (
        # Composite FK: (organization_id, remediation_run_id) → remediation_runs.
        # RESTRICT: a decision row must never silently disappear alongside
        # the run it belongs to. RemediationRun rows are only ever deleted
        # transitively (Organization cascade → TaskRun cascade → RemediationRun
        # cascade), so RESTRICT is never triggered in practice but closes the
        # schema-level gap, consistent with RemediationChange's own FK
        # ondelete=RESTRICT convention.
        ForeignKeyConstraint(
            ["organization_id", "remediation_run_id"],
            ["remediation_runs.organization_id", "remediation_runs.id"],
            name="fk_remediation_change_decisions_org_run",
            ondelete="RESTRICT",
        ),
        # Note: no composite FK (organization_id, remediation_change_id) →
        # remediation_changes here. remediation_changes has no
        # UNIQUE(organization_id, id) constraint (unlike remediation_runs which
        # does), so SQLite rejects a composite FK reference. The simple FK on
        # remediation_change_id below provides referential integrity to the PK.
        # Tenant isolation is already enforced by organization_id → organizations.id.
        # Required so this table can be referenced as a composite FK target
        # by a future table using the (organization_id, id) pattern that every
        # other parent table in this project exposes.
        UniqueConstraint("organization_id", "id", name="uq_remediation_change_decisions_org_id"),
        # No UNIQUE(organization_id, remediation_change_id) -- deliberately
        # omitted. Multiple rows per change are allowed (append-only history);
        # the API enforces one decision per change per Module 16, but the
        # schema leaves the future admin-override path open. See Section 2a
        # and Section 4 of the design doc.
        CheckConstraint(
            "decision IN ("
            + ", ".join(f"'{v}'" for v in REMEDIATION_CHANGE_DECISION_VALUES)
            + ")",
            name="ck_remediation_change_decisions_valid",
        ),
        # Composite index for efficient "latest decision per change" queries.
        # Supports both the single-change lookup (ORDER BY decision_timestamp DESC
        # LIMIT 1) and the run-level aggregate (DISTINCT ON remediation_change_id
        # ORDER BY remediation_change_id, decision_timestamp DESC).
        Index(
            "ix_remediation_change_decisions_org_chg_ts",
            "organization_id",
            "remediation_change_id",
            "decision_timestamp",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalized from RemediationChange for efficient run-scoped aggregate
    # queries without an extra join through remediation_changes -- same
    # pattern RemediationChange itself uses to denormalize remediation_run_id.
    remediation_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    remediation_change_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey(
            "remediation_changes.id",
            ondelete="RESTRICT",
            name="fk_remediation_change_decisions_org_change",
        ),
        nullable=False,
        index=True,
    )

    # "approved" or "rejected" -- enforced by ck_remediation_change_decisions_valid
    # and cross-checked at import time against REMEDIATION_CHANGE_DECISION_VALUES
    # in app.models.enums.
    decision: Mapped[str] = mapped_column(String(10), nullable=False)

    # Reviewer identity -- three complementary columns:
    # reviewer_id: FK for referential integrity (SET NULL on user delete)
    # reviewer_name: snapshot of User.full_name (or email if full_name is NULL)
    #   at decision time -- survives user deletion or profile rename
    # reviewer_role: snapshot of reviewer capacity at decision time
    #   ("superuser" if User.is_superuser, "user" otherwise) -- nullable to
    #   accommodate future role systems that may replace this binary flag
    reviewer_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("users.id", ondelete="SET NULL", name="fk_remediation_change_decisions_reviewer"),
        nullable=True,
    )
    reviewer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewer_role: Mapped[str | None] = mapped_column(String(100), nullable=True)

    # Set by the API handler, not server-side -- same convention as
    # CleaningRun.approved_at, StandardizationRun.approved_at, etc.
    decision_timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )

    comment: Mapped[str | None] = mapped_column(Text(), nullable=True)

    # Apply fields: always NULL in Module 16. Reserved for the future
    # Apply/Export module that will mark which approved proposals were
    # actually executed against the source data. applied_by follows the
    # same SET NULL convention as reviewer_id.
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    applied_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey(
            "users.id",
            ondelete="SET NULL",
            name="fk_remediation_change_decisions_applied_by",
        ),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Relationships -- no back_populates on RemediationChange or
    # RemediationRun since those Module 15 models are not touched in
    # Module 16 (additive-only rule). Forward references are sufficient
    # for queries that start from this table.
    remediation_run: Mapped["RemediationRun"] = relationship(  # noqa: F821
        foreign_keys=[organization_id, remediation_run_id],
    )
    remediation_change: Mapped["RemediationChange"] = relationship(  # noqa: F821
        foreign_keys=[organization_id, remediation_change_id],
    )

    def __repr__(self) -> str:
        return (
            f"RemediationChangeDecision("
            f"id={self.id!r}, "
            f"change={self.remediation_change_id!r}, "
            f"decision={self.decision!r}, "
            f"ts={self.decision_timestamp!r})"
        )
