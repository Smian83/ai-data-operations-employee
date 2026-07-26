"""
RemediationRun: immutable summary produced for one Module 15 REMEDIATE
TaskRun. Direct structural sibling of IssueDetectionRun (Module 14), not
of CleaningRun -- no status column, no approval fields, never updated
after creation. The engine only ever computes and persists PROPOSED
corrections; it never writes a modified copy of the dataset and never
mutates the source file, so there is no output to gate and nothing to
approve, reject, or roll back. See
docs/module-15-deterministic-cleaning-engine-design.md.

Consumes exactly one Module 14 IssueDetectionRun (via source_task_run_id,
denormalized from TaskRun.source_task_run_id, same convention every prior
chained module uses) belonging to the same organization_id -- verified by
app.worker.handlers.remediation, which also verifies the upstream run's
source_sha256 still matches the current source file before proposing any
change (see IssueDetectionRun's own docstring). No "approved" gate is
required upstream, since IssueDetectionRun has no approval concept at all.

issues_considered_count / total_changes_count / issues_skipped_count are
always the true counts for this run: every considered Issue produces
either exactly one RemediationChange or is recorded as skipped -- never
both, never neither (enforced below by
ck_remediation_runs_counts_reconcile). total_changes_count is also always
equal to how many RemediationChange rows actually exist for this run,
since -- unlike CleaningRun/IssueDetectionRun -- Module 15 has no
persisted-vs-total-found capping concept of its own: issues_considered_count
is already bounded by whatever Module 14 persisted for the upstream run
(see settings.issue_detection_max_persisted_issues), and every considered
Issue is cheap enough (one deterministic function call) that capping a
second time here was judged unnecessary. settings.
remediation_max_persisted_changes exists as a defensive ceiling only (see
app.worker.handlers.remediation) and is not expected to bind in practice.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
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


class RemediationRun(Base):
    __tablename__ = "remediation_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_remediation_runs_org_task_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_remediation_runs_org_task",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_remediation_runs_org_data_source",
            ondelete="RESTRICT",
        ),
        # The Module 14 DETECT run this RemediationRun consumed.
        ForeignKeyConstraint(
            ["organization_id", "source_task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_remediation_runs_org_source_task_run",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("task_run_id", name="uq_remediation_runs_task_run_id"),
        # Required so RemediationChange can have a composite FK
        # (organization_id, remediation_run_id) -> (organization_id, id),
        # same pattern as every other parent-of-child-audit-rows table.
        UniqueConstraint("organization_id", "id", name="uq_remediation_runs_org_id"),
        CheckConstraint(
            "issues_considered_count >= 0",
            name="ck_remediation_runs_considered_nonneg",
        ),
        CheckConstraint(
            "total_changes_count >= 0", name="ck_remediation_runs_changes_nonneg"
        ),
        CheckConstraint(
            "issues_skipped_count >= 0", name="ck_remediation_runs_skipped_nonneg"
        ),
        CheckConstraint(
            "total_changes_count + issues_skipped_count = issues_considered_count",
            name="ck_remediation_runs_counts_reconcile",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    data_source_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    source_task_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)

    # How many Issue rows were fetched from the upstream IssueDetectionRun
    # (bounded by whatever Module 14 actually persisted -- see
    # IssueDetectionRun.persisted_issue_count -- never total_issues_found).
    issues_considered_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    # How many of those produced a RemediationChange row.
    total_changes_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    # How many were considered but produced no safe deterministic proposal
    # (unconfigured column, ambiguous input, reused function returned
    # "unchanged", etc.) -- stored explicitly rather than left as an
    # implied subtraction, matching every other run table's "the summary
    # is always pre-computed and exact" convention.
    issues_skipped_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    # {action: count}, only keys with at least one proposal present -- see
    # app.models.enums.REMEDIATION_ACTIONS.
    changes_by_action: Mapped[dict] = mapped_column(JSON, nullable=False)
    # {skip_reason: count}, only keys with at least one skipped Issue
    # present -- see app.remediation.skip_reasons. Module 15 Phase 4
    # addition: the pure engine already computes a reason per skipped
    # Issue (RemediationResult.skipped, each a SkippedIssue with its own
    # .reason), but Phase 3 only ever persisted the total
    # (issues_skipped_count) -- this column is the same aggregate-and-
    # persist treatment changes_by_action already got, added so the Phase
    # 4 read-only summary endpoint can report a real skip-reason
    # breakdown without ever re-running the engine at read time.
    skipped_by_reason: Mapped[dict] = mapped_column(JSON, nullable=False)

    remediation_engine_version: Mapped[str] = mapped_column(String(20), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Module 15: explicit foreign_keys required -- unlike IssueDetectionRun
    # (which has only one FK into task_runs), this table has TWO
    # (task_run_id AND source_task_run_id, both -> task_runs), so
    # SQLAlchemy cannot infer which one this relationship should join on
    # without disambiguation.
    task_run: Mapped["TaskRun"] = relationship(  # noqa: F821
        back_populates="remediation_run",
        foreign_keys=[organization_id, task_run_id],
    )
    # overlaps="remediation_changes": both this relationship and
    # Issue.remediation_changes write RemediationChange.organization_id (via
    # two different FK constraints that both include that column -- the
    # ordinary tenant-scoping column, always identical either way). Same
    # documented pattern Module 8's MatchDecision/MatchGroup/MatchRun
    # already established for the same "two overlapping composite FKs share
    # organization_id" shape.
    changes: Mapped[list["RemediationChange"]] = relationship(  # noqa: F821
        back_populates="remediation_run",
        order_by="RemediationChange.row_number",
        overlaps="remediation_changes",
    )

    def __repr__(self) -> str:
        return (
            f"RemediationRun(id={self.id!r}, task_run={self.task_run_id!r}, "
            f"total_changes_count={self.total_changes_count!r})"
        )
