"""The pure Quality Control Engine: quality_control(inputs) -> QualityEngineResult.

No I/O, no randomness, no database/session access anywhere in this module —
exact mirror of app.validation.engine and app.remediation.engine.
All file/DB I/O happens only in Phase 3's QualityControlHandler.

Deterministic execution: category order is fixed (QUALITY_RULES declaration
order), finding order is deterministic (category ASC, rule_name ASC,
affected_column ASC — ties broken by a fixed secondary key). Same input set
always produces byte-identical output. This is verified by
tests/test_quality_repeatability.py.

Release decision evaluation order (Section 10 of the architecture document):
  FAIL if ANY of:
    1. overall_score IS NULL (no applicable categories)
    2. blocking_count > 0
    3. overall_score < fail_score_threshold
    4. failed_count / approved_changes_considered > max_validation_failure_rate
    5. unresolved_critical_count > max_critical_severity_unresolved
    6. unresolved_high_count > max_high_severity_unresolved
  PASS if ALL of:
    1. No FAIL condition triggered
    2. overall_score >= pass_score_threshold
    3. warning_count <= max_warnings_for_clean_pass
  PASS_WITH_WARNINGS — all other cases (no FAIL, but not a clean PASS).

QUALITY_ENGINE_VERSION is bumped when engine-level logic changes in a way
that could change the overall outcome for identical inputs. Per-category
version changes are captured in each category's own category_rule_version
(stored on QualityFinding.rule_version) and do not bump this constant.
"""
from __future__ import annotations

from app.quality.reasons import NO_APPLICABLE_CATEGORIES
from app.quality.registry import QUALITY_RULES
from app.quality.types import (
    QualityEngineInput,
    QualityEngineResult,
    QualityFindingResult,
)

QUALITY_ENGINE_VERSION = "1.0"

# Category name used on the special meta-finding emitted when no categories
# are applicable. This is a system-level finding, not tied to any of the 8
# quality categories (Section 11 of the architecture document).
_META_CATEGORY = "quality_control"


def _effective_weight(rule_category_name: str, default_weight: int,
                      weight_overrides: dict[str, int] | None) -> int:
    """Return the effective weight for a category.

    If weight_overrides contains a positive entry for this category, use it.
    Otherwise fall back to the category's default_weight. The handler has
    already validated that at least one active category has weight > 0, so
    the total weight_sum passed to the caller is guaranteed > 0.
    """
    if weight_overrides and rule_category_name in weight_overrides:
        override = weight_overrides[rule_category_name]
        if override > 0:
            return override
    return default_weight


def _category_status(score: float, findings: tuple[QualityFindingResult, ...]) -> str:
    """Compute category status from score and findings (Section 8.3).

    passed:  score ≥ 85.0 AND zero blocking or warning findings.
    warning: score ≥ 60.0 AND zero blocking findings AND ≥ 1 warning finding.
    failed:  score < 60.0 OR ≥ 1 blocking finding.
    """
    has_blocking = any(f.severity == "blocking" for f in findings)
    has_warning = any(f.severity == "warning" for f in findings)

    if has_blocking or score < 60.0:
        return "failed"
    if score >= 85.0 and not has_warning:
        return "passed"
    # score ≥ 60.0 and (score < 85.0 OR has_warning) → "warning"
    return "warning"


def _finding_sort_key(f: QualityFindingResult) -> tuple[str, str, str, str]:
    """Stable, deterministic sort key for findings.

    Ordered by: category ASC, rule_name ASC, affected_column ASC (None last),
    reason ASC (tie-breaker). This matches the stable ordering the API layer
    uses when listing findings, so the bounded tuple stored in the result is
    already in display order.
    """
    return (
        f.category,
        f.rule_name,
        f.affected_column or "\xff",  # None sorts after any real column name
        f.reason,
    )


def _compute_recommendation(
    overall_score: float | None,
    blocking_count: int,
    warning_count: int,
    inputs: QualityEngineInput,
) -> str:
    """Evaluate FAIL / PASS / PASS_WITH_WARNINGS in the exact order defined
    in Section 10 of the architecture document. The first matching condition
    wins; no exception.
    """
    thresholds = inputs.thresholds
    effective_stats = inputs.effective_stats
    val_run = inputs.validation_run

    # ── FAIL conditions ───────────────────────────────────────────────────────
    # 1. No applicable categories.
    if overall_score is None:
        return "FAIL"

    # 2. Any blocking finding.
    if blocking_count > 0:
        return "FAIL"

    # 3. Score below fail threshold.
    if overall_score < thresholds.fail_score_threshold:
        return "FAIL"

    # 4. Validation failure rate exceeded.
    if val_run.approved_changes_considered > 0:
        failure_rate = val_run.failed_count / val_run.approved_changes_considered
        if failure_rate > thresholds.max_validation_failure_rate:
            return "FAIL"

    # 5. Too many unresolved critical-severity issues.
    if effective_stats.unresolved_critical_count > thresholds.max_critical_severity_unresolved:
        return "FAIL"

    # 6. Too many unresolved high-severity issues.
    if effective_stats.unresolved_high_count > thresholds.max_high_severity_unresolved:
        return "FAIL"

    # ── PASS conditions ───────────────────────────────────────────────────────
    if (
        overall_score >= thresholds.pass_score_threshold
        and warning_count <= thresholds.max_warnings_for_clean_pass
    ):
        return "PASS"

    # ── PASS_WITH_WARNINGS ────────────────────────────────────────────────────
    return "PASS_WITH_WARNINGS"


def quality_control(inputs: QualityEngineInput) -> QualityEngineResult:
    """Pure deterministic quality control engine.

    inputs   -- all data needed to evaluate quality: ValidationRun snapshot,
                ValidationResult snapshots, IssueDetectionRun snapshot, Issue
                snapshots, DataProfile snapshot, effective post-remediation
                statistics, threshold configuration, and finding limits.

    Returns a QualityEngineResult where:
      - Every applicable category has a score in category_scores.
      - Every category (applicable or skipped) has a status in category_statuses.
      - Only applicable categories appear in category_weights_used.
      - findings is bounded to limits.max_persisted_findings (stable-sorted).
      - total_findings == blocking_count + warning_count + info_count (invariant).
      - overall_score is None when no categories are applicable.
      - recommendation is PASS / PASS_WITH_WARNINGS / FAIL.
    """
    weight_overrides = inputs.thresholds.category_weights

    # Per-category accumulators.
    all_findings: list[QualityFindingResult] = []
    category_scores: dict[str, float] = {}
    category_statuses: dict[str, str] = {}
    category_weights_used: dict[str, float] = {}

    # For overall score computation.
    weighted_score_sum = 0.0
    weight_sum = 0

    for rule in QUALITY_RULES:
        if not rule.is_applicable(inputs):
            category_statuses[rule.category_name] = "skipped"
            continue

        result = rule.evaluate(inputs)
        score = result.score
        findings = result.findings

        status = _category_status(score, findings)
        eff_weight = _effective_weight(
            rule.category_name, rule.default_weight, weight_overrides
        )

        category_scores[rule.category_name] = score
        category_statuses[rule.category_name] = status
        category_weights_used[rule.category_name] = float(eff_weight)

        weighted_score_sum += score * eff_weight
        weight_sum += eff_weight
        all_findings.extend(findings)

    # Compute overall score (nullable when no applicable categories).
    if weight_sum == 0:
        overall_score: float | None = None
        # Emit the meta-finding for no applicable categories (Section 11).
        no_cat_finding = QualityFindingResult(
            category=_META_CATEGORY,
            rule_name="no_applicable_categories",
            rule_version=QUALITY_ENGINE_VERSION,
            severity="blocking",
            outcome="failed",
            reason=NO_APPLICABLE_CATEGORIES,
        )
        all_findings = [no_cat_finding]
    else:
        overall_score = weighted_score_sum / weight_sum

    # Count findings before applying the limit.
    total_findings = len(all_findings)
    blocking_count = sum(1 for f in all_findings if f.severity == "blocking")
    warning_count = sum(1 for f in all_findings if f.severity == "warning")
    info_count = sum(1 for f in all_findings if f.severity == "info")

    # Verify count-reconcile invariant.
    assert blocking_count + warning_count + info_count == total_findings, (
        "quality_control() invariant violated: "
        f"blocking({blocking_count}) + warning({warning_count}) + "
        f"info({info_count}) != total_findings({total_findings})"
    )

    # Compute release recommendation.
    recommendation = _compute_recommendation(
        overall_score, blocking_count, warning_count, inputs
    )

    # Sort findings into deterministic order, then apply the cap.
    sorted_findings = sorted(all_findings, key=_finding_sort_key)
    persisted_findings = tuple(sorted_findings[: inputs.limits.max_persisted_findings])

    return QualityEngineResult(
        overall_score=overall_score,
        recommendation=recommendation,
        findings=persisted_findings,
        total_findings=total_findings,
        blocking_count=blocking_count,
        warning_count=warning_count,
        info_count=info_count,
        category_scores=category_scores,
        category_statuses=category_statuses,
        category_weights_used=category_weights_used,
    )
