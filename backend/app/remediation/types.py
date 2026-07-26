"""Internal immutable value objects shared by the remediation engine and
every action. Mirrors app.detection.types / app.cleaning.types' shape
exactly -- pure dataclasses, no I/O, no database/session references
anywhere in this module."""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RemediationDataset:
    """The read-only dataset the Issues being remediated were found in.
    Deliberately NOT the source of any proposed value -- every action
    reads original_value from the RemediationIssueInput itself, never from
    dataset.rows (see this package's own docstring and the Module 15
    design doc Section 5: "no ordering dependency between actions" would
    break immediately if a value were re-derived from a mutable "current
    row state"). Its one legitimate use in this engine is the defensive
    row-existence check in engine.py: an Issue whose row_number no longer
    falls inside this dataset is skipped (ROW_NOT_FOUND_IN_DATASET), never
    silently processed against a value that no longer exists."""

    headers: list[str]
    rows: list[list[str]]


@dataclass(frozen=True)
class RemediationIssueInput:
    """Read-only view of one persisted Module 14 Issue -- exactly the
    fields the pure engine needs, nothing ORM-specific. issue_id is
    mandatory: it becomes RemediationChangeProposal.source_issue_id (and
    later RemediationChange.source_issue_id) verbatim, since Module 15
    decision 6 requires every proposal to trace to exactly one Issue. The
    ORM row this was read from, and the DB session that read it, are never
    something this package touches -- a later RemediationHandler builds
    these from real Issue rows before calling remediate()."""

    issue_id: uuid.UUID
    row_number: int
    column_name: str | None
    issue_type: str
    original_value: str | None
    suggested_fix: str | None


@dataclass(frozen=True)
class RemediationColumnConfig:
    """One column's resolved governance, keyed by column_name in the
    column_config dict passed to remediate(). Two distinct source tables
    are merged here, both read-only and never duplicated:
      - source_date_format / default_country / capitalization_target come
        from RemediationColumnRule (Module 15's own config table).
      - allowed_values comes from IssueDetectionColumnRule.allowed_values
        (Module 14's config table) -- Module 15 never copies or
        re-declares this configuration; normalize_enum_value reads it
        directly, exactly as the design doc's Section 3/4 requires.
    A column with no row in either table is simply absent from the
    column_config dict -- every field below then implicitly means "no
    gate satisfied, never guess", matching app.detection.types.
    ColumnRuleConfig's identical "absent = no check" convention.

    target_date_format is a second, independent strftime format string --
    normalize_date requires BOTH source_date_format (how to parse) AND
    target_date_format (how to render) to be explicitly configured before
    proposing anything. Never inferred from source_date_format's own
    shape (that would be a guess): a date-only target_date_format (e.g.
    "%Y-%m-%d") produces a date-only output; a target_date_format with
    time directives produces a datetime output, 00:00:00 appearing only
    when the parsed value truly has no time component AND the configured
    target format asks for one -- never forced when the target format
    itself is date-only."""

    source_date_format: str | None = None
    target_date_format: str | None = None
    default_country: str | None = None
    capitalization_target: str | None = None
    allowed_values: tuple[str, ...] | None = None


@dataclass(frozen=True)
class RemediationDatasetConfigInput:
    """The resolved RemediationDatasetConfig for one data source. Always
    present (never None) when passed to remediate() -- a data source with
    no active config row simply resolves to both flags False (the
    documented default-off behavior), built by a later RemediationHandler,
    not by this dataclass."""

    remove_duplicate_rows_enabled: bool = False
    remove_duplicate_primary_keys_enabled: bool = False


@dataclass(frozen=True)
class RemediationLimits:
    # Defensive ceiling only (see app.remediation.engine's own docstring
    # on how overflow is handled) -- not expected to bind in practice,
    # per the Module 15 design doc Section 3.
    max_persisted_changes: int


@dataclass(frozen=True)
class RuleOutcome:
    """One RemediationRule's verdict for one Issue. `changed` is the only
    field callers should branch on -- proposed_value being None does NOT
    mean "no change" (the two duplicate-removal actions propose exclusion
    via proposed_value=None as a genuine change), so this is a real
    boolean field, not inferred from nullability. Exactly one of
    (reason) / (skip_reason) is ever set, matching `changed`."""

    changed: bool
    proposed_value: str | None = None
    reason: str | None = None
    skip_reason: str | None = None

    def __post_init__(self) -> None:
        if self.changed and self.reason is None:
            raise AssertionError("RuleOutcome(changed=True) requires reason")
        if not self.changed and self.skip_reason is None:
            raise AssertionError("RuleOutcome(changed=False) requires skip_reason")


@dataclass(frozen=True)
class RemediationChangeProposal:
    """One proposed correction -- exactly the fields a later
    RemediationHandler will persist onto a RemediationChange row verbatim,
    minus id/organization_id/remediation_run_id/created_at, which only
    exist once persisted. confidence is always 1.0 (rule-based only, per
    the Module 15 requirements contract) -- stored as a real field rather
    than hardcoded downstream so this dataclass's shape matches
    RemediationChange's own column shape exactly."""

    source_issue_id: uuid.UUID
    row_number: int
    column_name: str | None
    action: str
    original_value: str | None
    proposed_value: str | None
    reason: str
    confidence: float = 1.0


@dataclass(frozen=True)
class SkippedIssue:
    """One considered Issue that produced no RemediationChange. Not
    persisted anywhere as its own row (only RemediationRun.
    issues_skipped_count is stored) -- exposed here so the pure engine's
    reasoning is fully testable, and so a later phase could log/report it
    without recomputing anything."""

    source_issue_id: uuid.UUID
    row_number: int
    column_name: str | None
    issue_type: str
    reason: str


@dataclass(frozen=True)
class RemediationResult:
    # Every considered Issue ends up in exactly one of these two lists --
    # never both, never neither (engine.py's own invariant, mirrored by
    # ck_remediation_runs_counts_reconcile at the database layer).
    changes: list[RemediationChangeProposal] = field(default_factory=list)
    skipped: list[SkippedIssue] = field(default_factory=list)
    issues_considered_count: int = 0
    total_changes_count: int = 0
    issues_skipped_count: int = 0
    changes_by_action: dict[str, int] = field(default_factory=dict)
