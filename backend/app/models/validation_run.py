"""
ValidationRun: immutable summary produced for one Module 17 VALIDATE
TaskRun. Direct structural sibling of IssueDetectionRun (Module 14) and
RemediationRun (Module 15) -- one row per TaskRun, no status column, no
approval fields, never updated after creation. The engine never modifies
any source data, approved change, or upstream row, so there is nothing
for a human to approve, reject, or roll back.

Consumes the approved RemediationChange proposals from a specific
RemediationRun (via source_task_run_id, denormalized from
TaskRun.source_task_run_id -- same convention every prior chained module
uses). "Approved" is resolved once at the start of ValidationHandler
execution (Adjustment 1: snapshot freeze) and never re-queried during
the run's lifetime.

approved_changes_considered / passed_count / failed_count / skipped_count
are always the true counts for this run: every approved change the
snapshot included ends up in exactly one of passed/failed/skipped --
never both, never neither (enforced by
ck_validation_runs_counts_reconcile). results_by_rule is the same
{rule_name: count} breakdown RemediationRun.changes_by_action established
for Module 15 -- only rule names with at least one result are present.

No processing_duration_ms column -- same decision as RemediationRun: this
is derived at the API layer from TaskRun.started_at/finished_at, never
stored. See docs/module-17-validation-engine-design.md.
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


class ValidationRun(Base):
    __tablename__ = "validation_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_validation_runs_org_task_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_validation_runs_org_task",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_validation_runs_org_data_source",
            ondelete="RESTRICT",
        ),
        # The Module 15 RemediationRun whose approved changes this run validated.
        # RESTRICT: a ValidationRun audit row must not silently disappear
        # alongside the RemediationRun it references. Same ondelete=RESTRICT
        # convention as RemediationRun.fk_remediation_runs_org_source_task_run.
        ForeignKeyConstraint(
            ["organization_id", "remediation_run_id"],
            ["remediation_runs.organization_id", "remediation_runs.id"],
            name="fk_validation_runs_org_remediation_run",
            ondelete="RESTRICT",
        ),
        # Idempotency gate: one ValidationRun per VALIDATE TaskRun, same as
        # IssueDetectionRun and RemediationRun. IntegrityError on duplicate
        # task_run_id triggers the handler's catch-and-refetch path.
        UniqueConstraint("task_run_id", name="uq_validation_runs_task_run_id"),
        # Required so ValidationResult can have a composite FK
        # (organization_id, validation_run_id) -> (organization_id, id),
        # same pattern as every other parent-of-child-audit-rows table.
        UniqueConstraint("organization_id", "id", name="uq_validation_runs_org_id"),
        # Count reconciliation: every change in the snapshot ends up in
        # exactly one of passed/failed/skipped.
        CheckConstraint(
            "approved_changes_considered >= 0",
            name="ck_validation_runs_considered_nonneg",
        ),
        CheckConstraint(
            "passed_count >= 0",
            name="ck_validation_runs_passed_nonneg",
        ),
        CheckConstraint(
            "failed_count >= 0",
            name="ck_validation_runs_failed_nonneg",
        ),
        CheckConstraint(
            "skipped_count >= 0",
            name="ck_validation_runs_skipped_nonneg",
        ),
        CheckConstraint(
            "passed_count + failed_count + skipped_count = approved_changes_considered",
            name="ck_validation_runs_counts_reconcile",
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

    # The specific RemediationRun whose approved changes were validated.
    # Denormalized from TaskRun.source_task_run_id via the handler's
    # lookup of RemediationRun, then stored here for efficient run-scoped
    # queries without an extra join back through task_runs.
    remediation_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)

    # How many approved RemediationChange rows were in the snapshot at the
    # start of this run (Adjustment 1: snapshot frozen once, never re-read).
    # Bounded by settings.validation_max_persisted_results (defensive ceiling).
    approved_changes_considered: Mapped[int] = mapped_column(Integer(), nullable=False)
    # Outcome breakdowns -- always pre-computed by the handler at persist time.
    passed_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    skipped_count: Mapped[int] = mapped_column(Integer(), nullable=False)

    # {validation_rule: count} -- only rules with at least one result present,
    # same pattern as RemediationRun.changes_by_action.
    results_by_rule: Mapped[dict] = mapped_column(JSON, nullable=False)

    # Validation engine version -- bumped when engine-level logic changes.
    # Per-rule version is on ValidationResult.validation_rule_version
    # (Adjustment 2: independent per-rule versioning).
    validation_engine_version: Mapped[str] = mapped_column(String(20), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    task_run: Mapped["TaskRun"] = relationship(  # noqa: F821
        back_populates="validation_run",
        foreign_keys=[organization_id, task_run_id],
    )
    results: Mapped[list["ValidationResult"]] = relationship(  # noqa: F821
        back_populates="validation_run",
        order_by="ValidationResult.created_at",
        # passive_deletes=True: let the database-level ondelete=CASCADE handle
        # child row removal. Without this, SQLAlchemy tries to UPDATE child FKs
        # to NULL before deleting the parent, which fails because organization_id
        # is NOT NULL. Same pattern used by IssueDetectionRun.issues and
        # RemediationRun.changes.
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return (
            f"ValidationRun(id={self.id!r}, task_run={self.task_run_id!r}, "
            f"approved={self.approved_changes_considered!r}, "
            f"passed={self.passed_count!r}, failed={self.failed_count!r}, "
            f"skipped={self.skipped_count!r})"
        )
