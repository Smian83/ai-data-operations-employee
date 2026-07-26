"""Immutable value objects shared by the validation engine and every
validation rule. Mirrors app.remediation.types' structure exactly --
pure dataclasses, no I/O, no database/session references anywhere in
this module.

ValidationChangeInput is the read-only view of one approved
RemediationChange row that the engine considers. The handler builds
these from real RemediationChange rows after snapshotting the approved
set (Adjustment 1: snapshot frozen once before validate() is called,
never re-queried during the run).

ValidationColumnConfig is the per-column governance re-read at
validation time from the same source tables Module 15 uses
(RemediationColumnRule + IssueDetectionColumnRule.allowed_values). It
is intentionally re-read rather than preserved from the original
remediation run -- any configuration change between the remediation run
and the validation run is reflected in the result, which is exactly the
"validate what the rule would produce today" contract.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class ValidationChangeInput:
    """Read-only view of one approved RemediationChange that the pure
    validation engine processes. Exactly the fields needed; no ORM
    references. change_id becomes ValidationResult.remediation_change_id
    verbatim. source_issue_id is denormalized from the RemediationChange
    row and stored on ValidationResult for query convenience, same
    pattern as RemediationChange.source_issue_id on RemediationRun."""

    change_id: uuid.UUID
    source_issue_id: uuid.UUID
    row_number: int
    column_name: str | None
    action: str
    original_value: str | None
    proposed_value: str | None


@dataclass(frozen=True)
class ValidationColumnConfig:
    """Resolved governance for one column, provided by the handler before
    calling validate(). Parallel structure to RemediationColumnConfig --
    the same source tables are re-read at validation time. A column
    absent from the column_config dict produces column_config=None in the
    rule's validate() call, using the same "absent = no gate satisfied,
    skip" convention Module 15 established in app.remediation.types."""

    source_date_format: str | None = None
    target_date_format: str | None = None
    default_country: str | None = None
    capitalization_target: str | None = None
    allowed_values: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ValidationRuleOutcome:
    """One ValidationRule's verdict for one ValidationChangeInput.
    outcome is always one of VALIDATION_OUTCOMES ("passed", "failed",
    "skipped"). reason is always a constant from
    app.validation.reasons -- never a dynamically constructed string,
    same discipline as app.remediation.skip_reasons."""

    outcome: str  # one of VALIDATION_OUTCOMES
    reason: str   # one of app.validation.reasons constants

    def __post_init__(self) -> None:
        from app.models.enums import VALIDATION_OUTCOMES
        if self.outcome not in VALIDATION_OUTCOMES:
            raise AssertionError(
                f"ValidationRuleOutcome.outcome must be one of "
                f"{VALIDATION_OUTCOMES!r}, got {self.outcome!r}"
            )


@dataclass(frozen=True)
class ValidationResultItem:
    """One per approved change considered by the engine. The handler
    persists each item as a ValidationResult row verbatim, except for
    id/organization_id/remediation_run_id/created_at which only exist
    once persisted. Structural mirror of RemediationChangeProposal in
    app.remediation.types."""

    change_id: uuid.UUID
    source_issue_id: uuid.UUID
    row_number: int
    column_name: str | None
    validation_rule: str          # one of VALIDATION_RULE_NAMES (or "" for unrecognised action)
    outcome: str                  # one of VALIDATION_OUTCOMES
    reason: str                   # one of app.validation.reasons constants
    original_value: str | None
    proposed_value: str | None
    validation_rule_version: str  # Adjustment 2: per-rule independent version


@dataclass(frozen=True)
class ValidationLimits:
    """Defensive ceiling matching settings.validation_max_persisted_results
    (app.core.config). Not expected to bind in practice -- same rationale
    and convention as RemediationLimits in app.remediation.types."""

    max_persisted_results: int


@dataclass(frozen=True)
class ValidationRunResult:
    """What validate() returns. The handler builds a ValidationRun row
    from the aggregate counts and persists each item as a
    ValidationResult row. Every approved change in the snapshot ends up
    in exactly one of passed/failed/skipped, never both, never neither
    -- mirrors RemediationResult's own invariant (verified by the engine
    and by ck_validation_runs_counts_reconcile at the database layer)."""

    results: list[ValidationResultItem] = field(default_factory=list)
    approved_changes_considered: int = 0
    passed_count: int = 0
    failed_count: int = 0
    skipped_count: int = 0
    # {validation_rule: result_count} -- only rules with at least one
    # result present; same pattern as RemediationRun.changes_by_action.
    results_by_rule: dict[str, int] = field(default_factory=dict)
