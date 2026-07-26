"""Immutable value objects for the Quality Control Engine (Module 18 Phase 2).

All types are frozen dataclasses — no mutable state anywhere. The handler
(Phase 3) builds QualityEngineInput from ORM rows; the engine consumes it
and returns QualityEngineResult. No ORM, no session, no I/O in this module.

Design mirrors app.validation.types exactly:
  ValidationChangeInput      ↔  ValidationResultSnapshot
  ValidationRunResult        ↔  QualityEngineResult
  ValidationLimits           ↔  QualityLimits
  ValidationColumnConfig     ↔  QualityThresholdConfig
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Input snapshots — read-only projections of ORM rows the handler provides.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ValidationResultSnapshot:
    """Read-only view of one ValidationResult row needed by the engine.

    outcome is one of VALIDATION_OUTCOMES ("passed", "failed", "skipped").
    rule_name is one of VALIDATION_RULE_NAMES.
    column_name is None for row-level rules (remove_duplicate_row, etc.).
    """

    id: uuid.UUID
    outcome: str
    rule_name: str
    column_name: str | None
    source_issue_id: uuid.UUID | None
    remediation_change_id: uuid.UUID | None


@dataclass(frozen=True)
class ValidationRunSnapshot:
    """Aggregate statistics from one completed ValidationRun row.

    results_by_rule: {rule_name: total_result_count} — mirrors
    ValidationRun.results_by_rule. Only rules with ≥1 result are present.
    """

    id: uuid.UUID
    approved_changes_considered: int
    passed_count: int
    failed_count: int
    skipped_count: int
    results_by_rule: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class IssueSnapshot:
    """Read-only view of one Issue row needed by the engine.

    severity is one of ISSUE_SEVERITIES ("INFO", "LOW", "MEDIUM", "HIGH",
    "CRITICAL") — uppercase, matching the enums.py constant.
    issue_type is one of the detection rule names (e.g. "missing_value").
    """

    id: uuid.UUID
    severity: str
    issue_type: str


@dataclass(frozen=True)
class IssueDetectionRunSnapshot:
    """Aggregate statistics from one completed IssueDetectionRun row."""

    id: uuid.UUID
    total_issues_found: int


@dataclass(frozen=True)
class DataProfileSnapshot:
    """Read-only view of the DataProfile row that is the quality baseline.

    row_count and column_count are from the raw (pre-remediation) source
    file. effective_* values are computed in EffectiveDatasetStats.
    source_sha256 is the SHA-256 of the source file — stored in
    execution_snapshot.inputs.data_profile_source_sha256 by the handler.
    """

    id: uuid.UUID
    row_count: int
    column_count: int
    duplicate_row_count: int
    missing_value_total: int
    source_sha256: str


@dataclass(frozen=True)
class EffectiveDatasetStats:
    """Computed post-remediation statistics derived by the handler (Phase 3).

    These are the effective values AFTER applying approved+passed
    RemediationChange deltas on top of the DataProfile baseline. The
    derivation algorithm is defined in Section 3.2 of the architecture doc.

    unresolved_high_count: count of HIGH-severity Issues NOT in
        addressed_issue_ids (i.e., without a passed ValidationResult).
    unresolved_critical_count: same for CRITICAL-severity Issues.
    addressed_issue_count: count of Issues that have at least one
        passed ValidationResult (via source_issue_id linkage).
    """

    effective_row_count: int
    effective_duplicate_row_count: int
    effective_missing_value_total: int
    unresolved_high_count: int
    unresolved_critical_count: int
    addressed_issue_count: int


@dataclass(frozen=True)
class QualityThresholdConfig:
    """Resolved threshold configuration passed to the engine.

    Built from QualityThreshold ORM row (data-source-specific or org-wide)
    or from built-in defaults when no row exists. The handler validates all
    fields before calling the engine (invalid config → PermanentExecutionError).

    threshold_config_id: None when built-in defaults are used.
    threshold_config_source: one of "data_source_specific", "org_wide",
        "built_in_defaults" — recorded in execution_snapshot.
    category_weights: {category_name: weight_override}. Missing keys use the
        category's own default_weight. None means no overrides.
    """

    fail_score_threshold: float = 60.0
    pass_score_threshold: float = 85.0
    max_validation_failure_rate: float = 0.0
    max_validation_skip_rate: float = 0.5
    max_high_severity_unresolved: int = 0
    max_critical_severity_unresolved: int = 0
    max_warnings_for_clean_pass: int = 0
    category_weights: dict[str, int] | None = None
    threshold_config_id: uuid.UUID | None = None
    threshold_config_source: str = "built_in_defaults"


@dataclass(frozen=True)
class QualityLimits:
    """Defensive ceiling for persisted findings.

    Matches settings.quality_max_persisted_findings (app.core.config).
    The engine caps the returned findings tuple to this size; total_findings
    reflects the true untruncated count — same bounded-but-never-silent
    pattern as validation_max_persisted_results.
    """

    max_persisted_findings: int


@dataclass(frozen=True)
class QualityEngineInput:
    """Everything the pure engine needs. No ORM references. No I/O.

    validation_results: tuple of all ValidationResult snapshots for the
        ValidationRun being evaluated, loaded once by the handler.
    issues: tuple of all Issue snapshots for the IssueDetectionRun,
        bounded by the same loading strategy as Module 14's API.
    effective_stats: precomputed by the handler (Section 3.2).
    thresholds: pre-validated by the handler (Section 9, Step 3).
    """

    organization_id: uuid.UUID
    data_source_id: uuid.UUID
    validation_run: ValidationRunSnapshot
    validation_results: tuple[ValidationResultSnapshot, ...]
    issue_detection_run: IssueDetectionRunSnapshot
    data_profile: DataProfileSnapshot
    effective_stats: EffectiveDatasetStats
    issues: tuple[IssueSnapshot, ...]
    thresholds: QualityThresholdConfig
    limits: QualityLimits


# ---------------------------------------------------------------------------
# Output types — what the engine produces.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QualityFindingResult:
    """One finding produced by a QualityCategory.evaluate() call.

    The handler persists each finding as a QualityFinding row, adding
    organization_id, quality_control_run_id, id, and created_at. All other
    fields are set verbatim from this dataclass.

    severity: one of QUALITY_FINDING_SEVERITIES ("info", "warning", "blocking").
    outcome: one of QUALITY_FINDING_OUTCOMES ("passed", "failed", "skipped").
    reason: one of the closed constants in app.quality.reasons — never a
        dynamically constructed string (same discipline as validation.reasons).
    """

    category: str
    rule_name: str
    rule_version: str
    severity: str
    outcome: str
    reason: str
    affected_row_count: int | None = None
    affected_column: str | None = None
    source_issue_id: uuid.UUID | None = None
    remediation_change_id: uuid.UUID | None = None
    validation_result_id: uuid.UUID | None = None


@dataclass(frozen=True)
class CategoryEvaluationResult:
    """What QualityCategory.evaluate() returns for one applicable category.

    score is always in [0.0, 100.0]. findings are the raw, unsorted findings
    the category produced — sorting to stable order happens in the engine.
    """

    score: float
    findings: tuple[QualityFindingResult, ...]


@dataclass(frozen=True)
class QualityEngineResult:
    """What quality_control() returns. Consumed by the handler for persistence.

    findings: bounded tuple (len ≤ limits.max_persisted_findings), stable-sorted.
    total_findings: true count BEFORE the limit is applied. Always equals
        blocking_count + warning_count + info_count (count-reconcile invariant).

    category_scores: {category_name: score} — absent keys are skipped categories.
    category_statuses: {category_name: status} — ALL 8 categories always present.
    category_weights_used: {category_name: weight} — only applicable categories.
    """

    overall_score: float | None  # None when no applicable categories
    recommendation: str          # one of QUALITY_RELEASE_RECOMMENDATIONS
    findings: tuple[QualityFindingResult, ...]  # bounded, stable-sorted
    total_findings: int          # true total (may exceed len(findings))
    blocking_count: int
    warning_count: int
    info_count: int
    category_scores: dict[str, float]       # absent = skipped
    category_statuses: dict[str, str]       # all 8 always present
    category_weights_used: dict[str, float]  # only applicable categories
