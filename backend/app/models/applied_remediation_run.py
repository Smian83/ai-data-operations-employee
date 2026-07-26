"""
AppliedRemediationRun: immutable artifact record produced for one
APPLY_REMEDIATIONS TaskRun. Structural sibling of ExportRun (Module 9):
both write an output CSV artifact, both store output_file_path +
output_sha256, both are append-only, never updated after creation.

Unlike RemediationRun (Module 15, which never writes an output file),
AppliedRemediationRun IS the write-side: it records the materialized
post-remediation CSV that was produced by reading the ExportRun artifact
and applying only the approved RemediationChange proposals.

decisions_snapshot_hash: SHA-256 of the ordered, deterministic
representation of all approved RemediationChangeDecision rows that were
applied during this run. Used by Module 19 Gate 4 to detect whether the
decision set changed after this artifact was materialized -- if the hash
no longer matches the current approved decisions, Module 19 blocks rather
than exporting a stale artifact.

No status column (same pattern as RemediationRun, IssueDetectionRun, and
ValidationRun): AppliedRemediationRun is immutable after the single write
transaction. There is no approval state machine -- the AppliedRemediationRun
row IS the completed record. A failed APPLY_REMEDIATIONS TaskRun simply has
no AppliedRemediationRun row (same "missing = not done" convention as every
prior immutable-audit-row module).

Foreign-key linkage:
  task_run_id  -> task_runs (the APPLY_REMEDIATIONS TaskRun, CASCADE)
  remediation_run_id -> remediation_runs (RESTRICT: audit row must not
    silently disappear if the RemediationRun is somehow removed).
    Stored so ValidationHandler (Module 17) can resolve the RemediationRun
    chain without walking back through TaskRun.source_task_run_id.

Identifier length audit (all <= 63 bytes):
  uq_applied_remediation_runs_task_run_id          40
  uq_applied_remediation_runs_org_id               34
  fk_applied_remediation_runs_org_task_run         39
  fk_applied_remediation_runs_org_task             35
  fk_applied_remediation_runs_org_data_source      44  ← <=63 ✓
  fk_applied_remediation_runs_org_remediation_run  48  ← <=63 ✓
  ck_applied_remediation_runs_applied_count_nonneg 48  ← <=63 ✓
  ck_applied_remediation_runs_skipped_count_nonneg 48  ← <=63 ✓
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class AppliedRemediationRun(Base):
    __tablename__ = "applied_remediation_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_applied_remediation_runs_org_task_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_applied_remediation_runs_org_task",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_applied_remediation_runs_org_data_source",
            ondelete="RESTRICT",
        ),
        # RESTRICT: a ValidationRun / audit row that references this
        # AppliedRemediationRun must not silently disappear if the
        # RemediationRun is somehow removed. Same convention as
        # ValidationRun.fk_validation_runs_org_remediation_run.
        ForeignKeyConstraint(
            ["organization_id", "remediation_run_id"],
            ["remediation_runs.organization_id", "remediation_runs.id"],
            name="fk_applied_remediation_runs_org_remediation_run",
            ondelete="RESTRICT",
        ),
        # Idempotency gate: one AppliedRemediationRun per APPLY_REMEDIATIONS
        # TaskRun. IntegrityError on duplicate task_run_id triggers the
        # handler's catch-and-refetch path (same pattern as every prior module).
        UniqueConstraint(
            "task_run_id",
            name="uq_applied_remediation_runs_task_run_id",
        ),
        # Required so downstream modules (ValidationRun) can have a composite
        # FK (organization_id, applied_remediation_run_id) -> (organization_id,
        # id), same pattern as every other parent-of-child-audit-rows table.
        UniqueConstraint(
            "organization_id", "id",
            name="uq_applied_remediation_runs_org_id",
        ),
        CheckConstraint(
            "applied_change_count >= 0",
            name="ck_applied_remediation_runs_applied_count_nonneg",
        ),
        CheckConstraint(
            "skipped_change_count >= 0",
            name="ck_applied_remediation_runs_skipped_count_nonneg",
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

    # The Module 15 RemediationRun whose approved changes were applied.
    # Stored here (denormalized from the ExportRun + RemediationRun chain)
    # so ValidationHandler (Module 17) can look up the RemediationRun in
    # one SELECT rather than walking back through TaskRun.source_task_run_id.
    remediation_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)

    # Path to the written artifact and its SHA-256 checksum. Same convention
    # as ExportRun.output_file_path / output_sha256 -- both set on the single
    # write transaction and never updated afterward.
    output_file_path: Mapped[str] = mapped_column(Text(), nullable=False)
    output_sha256: Mapped[str] = mapped_column(String(64), nullable=False)

    # Deterministic hash of the approved-decision set applied in this run.
    # SHA-256 of a sorted, canonical representation of every applied
    # RemediationChangeDecision (change_id, decision) tuple. Used by Module 19
    # Gate 4 to detect decision-set drift after materialization.
    decisions_snapshot_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    # How many approved changes were actually applied to the ExportRun artifact.
    applied_change_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    # How many approved changes were present but could NOT be applied (e.g.,
    # row_number out of range for the ExportRun row count, or a remove-action
    # for an already-removed row -- permanently logged, never silently dropped).
    skipped_change_count: Mapped[int] = mapped_column(Integer(), nullable=False)

    # Engine version for forward compatibility / audit. Bumped when the apply
    # algorithm changes in a way that could produce different output for the
    # same inputs.
    apply_engine_version: Mapped[str] = mapped_column(String(20), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"AppliedRemediationRun(id={self.id!r}, "
            f"task_run={self.task_run_id!r}, "
            f"applied={self.applied_change_count!r}, "
            f"skipped={self.skipped_change_count!r})"
        )
