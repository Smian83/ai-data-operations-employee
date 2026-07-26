"""
ValidationResult: one structured outcome produced by a Module 17
ValidationRun for a single approved RemediationChange. Append-only,
immutable child row -- direct structural sibling of Issue (Module 14)
and RemediationChange (Module 15). Never written outside
app.worker.handlers.validation, and never updated or deleted once written.

Each row records whether the proposed value on an approved RemediationChange
satisfies the intended remediation rule. The three outcomes are:
  'passed'  -- proposed_value is valid for the action that proposed it.
  'failed'  -- proposed_value fails the validation rule (e.g., proposed
               trim of "  x  " yields " x " rather than "x").
  'skipped' -- the validator could not evaluate (missing config, missing
               original value, or other precondition not met); not a
               failure, not a pass.

Two version columns (Adjustment 2: independent per-rule versioning):
  validation_engine_version: the VALIDATION_ENGINE_VERSION constant from
    app.validation.engine at run time -- bumped when engine-level logic
    changes across all rules.
  validation_rule_version: the rule_version constant from the specific
    ValidationRule implementation that evaluated this change -- bumped
    independently per rule when that rule's logic changes, without
    requiring the engine version to change. This makes per-rule changes
    fully auditable at the result-row level.

Both version columns are denormalized onto every row so a row can be
audited in isolation, without joining to ValidationRun.

Encryption compatibility (Adjustment 4): reason is Text() (unbounded)
to accommodate future field-level encryption without a schema change.
Any future 'comment' or 'metadata' field added to this table must also
use Text(), never VARCHAR. No encryption is implemented in Module 17.

source_issue_id is denormalized from RemediationChange.source_issue_id:
required by the Module 17 specification; stored here to avoid a JOIN
on every API result read, same as RemediationChangeDecision
denormalizes remediation_run_id.

No UNIQUE(organization_id, remediation_change_id): deliberate omission.
A future re-validation pass creates new rows without erasing history.
The 'latest row wins' convention (by created_at) applies if multiple
ValidationRun rows exist for the same RemediationRun. See
docs/module-17-validation-engine-design.md Section 2b.
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
from app.models.enums import VALIDATION_OUTCOMES


class ValidationResult(Base):
    __tablename__ = "validation_results"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "validation_run_id"],
            ["validation_runs.organization_id", "validation_runs.id"],
            name="fk_validation_results_org_validation_run",
            ondelete="CASCADE",
        ),
        # Note: remediation_changes has no UNIQUE(organization_id, id), so a
        # composite FK is not possible (same situation as
        # remediation_change_decisions). Simple FK to the PK provides
        # referential integrity; tenant isolation is already enforced by
        # organization_id -> organizations.id.
        # RESTRICT: a ValidationResult audit row must never silently disappear
        # alongside the RemediationChange it references.
        ForeignKeyConstraint(
            ["remediation_change_id"],
            ["remediation_changes.id"],
            name="fk_validation_results_remediation_change",
            ondelete="RESTRICT",
        ),
        # Required so a future table can reference this one via a composite FK
        # (organization_id, id) -> (organization_id, id), same pattern as
        # every other parent-of-child-audit-rows table in this project.
        UniqueConstraint("organization_id", "id", name="uq_validation_results_org_id"),
        # No UNIQUE(organization_id, remediation_change_id): deliberate
        # omission -- a future re-validation pass inserts new rows without
        # erasing history. See module docstring above.
        CheckConstraint(
            "outcome IN ("
            + ", ".join(f"'{v}'" for v in VALIDATION_OUTCOMES)
            + ")",
            name="ck_validation_results_outcome_valid",
        ),
        # Composite index: supports "latest result per change" queries and
        # cross-org batch reads. outcome index (Adjustment 3) added separately
        # below via Index() so it appears as a named object in the migration.
        Index(
            "ix_validation_results_org_change_ts",
            "organization_id",
            "remediation_change_id",
            "created_at",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    validation_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)

    # Denormalized from RemediationChange for efficient run-scoped aggregate
    # queries without an extra join through validation_runs, same pattern as
    # RemediationChangeDecision.remediation_run_id.
    remediation_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)

    remediation_change_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), nullable=False, index=True
    )

    # Denormalized from RemediationChange.source_issue_id: required by the
    # Module 17 specification for every result row; stored here to avoid a
    # JOIN on every API result read.
    source_issue_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False)

    # Name of the ValidationRule that evaluated this change -- always one of
    # VALIDATION_RULE_NAMES in app.models.enums, cross-checked at import time
    # against VALIDATION_RULES in app.validation.registry (Phase 2).
    validation_rule: Mapped[str] = mapped_column(String(50), nullable=False)

    # 'passed' | 'failed' | 'skipped' -- enforced by
    # ck_validation_results_outcome_valid.
    outcome: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        index=True,  # ix_validation_results_outcome -- Adjustment 3
    )

    # Fixed-vocabulary reason string from app.validation.reasons. Text()
    # (unbounded) for encryption compatibility (Adjustment 4): future
    # field-level encryption must not require a schema change. Same
    # rationale as RemediationChangeDecision.comment (Module 16).
    reason: Mapped[str] = mapped_column(Text(), nullable=False)

    # From RemediationChange.original_value -- nullable because whole-row
    # removal actions (remove_duplicate_row, remove_duplicate_primary_key)
    # have no single original value to record.
    original_value: Mapped[str | None] = mapped_column(Text(), nullable=True)

    # From RemediationChange.proposed_value -- nullable for the same reason:
    # removal actions propose exclusion (proposed_value=None is correct for
    # those actions, not a data-quality gap).
    proposed_value: Mapped[str | None] = mapped_column(Text(), nullable=True)

    # Engine-level version -- bumped when validation engine logic changes
    # across all rules. Denormalized from VALIDATION_ENGINE_VERSION so a
    # result row is self-describing without joining to ValidationRun.
    validation_engine_version: Mapped[str] = mapped_column(String(20), nullable=False)

    # Rule-level version -- bumped independently per ValidationRule
    # implementation when that rule's logic changes (Adjustment 2). Allows
    # auditors to distinguish results produced by old vs. new rule versions
    # without comparing engine versions. Stored per row (not on ValidationRun)
    # because different rules can have different versions within the same run.
    validation_rule_version: Mapped[str] = mapped_column(String(20), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    validation_run: Mapped["ValidationRun"] = relationship(  # noqa: F821
        back_populates="results",
        foreign_keys=[organization_id, validation_run_id],
    )

    def __repr__(self) -> str:
        return (
            f"ValidationResult("
            f"id={self.id!r}, "
            f"change={self.remediation_change_id!r}, "
            f"rule={self.validation_rule!r}, "
            f"outcome={self.outcome!r})"
        )
