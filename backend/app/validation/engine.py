"""The pure validation engine: validate(changes, column_config, limits)
-> ValidationRunResult.

No I/O, no randomness, no database/session access anywhere in this
module -- exact mirror of app.remediation.engine and app.detection.engine.
All file/DB I/O happens only in Phase 3's ValidationHandler.

Deterministic execution: changes are processed in a fixed sort order
(row_number, column_name or "", action) regardless of the order the
caller passed them in, so the same change set + column_config always
produces byte-identical output. This is what
tests/test_validation_repeatability.py verifies.

Every approved change in the snapshot ends up in exactly one of
passed/failed/skipped -- never both, never neither. This unconditional
count-reconcile invariant mirrors ck_validation_runs_counts_reconcile
at the database layer (Phase 1 migration).

Persisted-result ceiling: limits.max_persisted_results is a defensive
ceiling (matching settings.validation_max_persisted_results from
app.core.config). If the ceiling is reached, all remaining changes are
classified as skipped with reasons.RESULT_LIMIT_REACHED rather than
silently dropped, preserving the count-reconcile invariant exactly as
PERSISTED_CHANGE_LIMIT_REACHED does in app.remediation.engine.

VALIDATION_ENGINE_VERSION is bumped when engine-level logic changes in
a way that could change the overall outcome for identical inputs. Per-rule
version changes are captured in each rule's own rule_version (Adjustment 2)
and stored on every ValidationResult row; VALIDATION_ENGINE_VERSION only
tracks this file and app.validation.registry."""
from __future__ import annotations

from app.validation.reasons import ACTION_NOT_RECOGNISED, RESULT_LIMIT_REACHED
from app.validation.registry import get_rule_for_action
from app.validation.types import (
    ValidationChangeInput,
    ValidationColumnConfig,
    ValidationLimits,
    ValidationResultItem,
    ValidationRunResult,
)

VALIDATION_ENGINE_VERSION = "1.0"


def _change_sort_key(change: ValidationChangeInput) -> tuple[int, str, str]:
    """Fixed, stable sort key: (row_number, column_name or "", action).
    Identical key shape to app.remediation.engine._issue_sort_key so the
    processing order of the two engines is consistent for the same set of
    rows and columns."""
    return (change.row_number, change.column_name or "", change.action)


def validate(
    changes: list[ValidationChangeInput],
    column_config: dict[str, ValidationColumnConfig],
    limits: ValidationLimits,
) -> ValidationRunResult:
    """Validate a frozen list of approved RemediationChange proposals.

    changes   -- the snapshot of approved changes (frozen before this
                 call; never re-queried or mutated by the engine).
    column_config -- keyed by column_name; None is passed to the rule
                 when a column has no entry (same absent=no-gate-satisfied
                 convention as app.remediation.engine).
    limits    -- max_persisted_results ceiling.

    Returns a ValidationRunResult where every change is in exactly one
    of passed/failed/skipped and the sum equals len(changes).
    """
    sorted_changes = sorted(changes, key=_change_sort_key)

    results: list[ValidationResultItem] = []
    passed_count = 0
    failed_count = 0
    skipped_count = 0
    results_by_rule: dict[str, int] = {}

    for change in sorted_changes:
        rule = get_rule_for_action(change.action)

        if rule is None:
            # Unrecognised action -- structural guard (should never fire
            # when the registry is complete, but never silently pass).
            item = ValidationResultItem(
                change_id=change.change_id,
                source_issue_id=change.source_issue_id,
                row_number=change.row_number,
                column_name=change.column_name,
                validation_rule="",
                outcome="skipped",
                reason=ACTION_NOT_RECOGNISED,
                original_value=change.original_value,
                proposed_value=change.proposed_value,
                validation_rule_version="",
            )
            results.append(item)
            skipped_count += 1
            continue

        if len(results) >= limits.max_persisted_results:
            # Ceiling reached -- classify remaining as skipped to preserve
            # the count-reconcile invariant (never silently drop a change).
            item = ValidationResultItem(
                change_id=change.change_id,
                source_issue_id=change.source_issue_id,
                row_number=change.row_number,
                column_name=change.column_name,
                validation_rule=rule.rule_name,
                outcome="skipped",
                reason=RESULT_LIMIT_REACHED,
                original_value=change.original_value,
                proposed_value=change.proposed_value,
                validation_rule_version=rule.rule_version,
            )
            results.append(item)
            skipped_count += 1
            # Still count this rule in results_by_rule (it was considered).
            results_by_rule[rule.rule_name] = (
                results_by_rule.get(rule.rule_name, 0) + 1
            )
            continue

        col_config = (
            column_config.get(change.column_name) if change.column_name else None
        )
        rule_outcome = rule.validate(change, col_config)

        item = ValidationResultItem(
            change_id=change.change_id,
            source_issue_id=change.source_issue_id,
            row_number=change.row_number,
            column_name=change.column_name,
            validation_rule=rule.rule_name,
            outcome=rule_outcome.outcome,
            reason=rule_outcome.reason,
            original_value=change.original_value,
            proposed_value=change.proposed_value,
            validation_rule_version=rule.rule_version,
        )
        results.append(item)

        if rule_outcome.outcome == "passed":
            passed_count += 1
        elif rule_outcome.outcome == "failed":
            failed_count += 1
        else:
            skipped_count += 1

        results_by_rule[rule.rule_name] = (
            results_by_rule.get(rule.rule_name, 0) + 1
        )

    # Count-reconcile invariant: every input change is in exactly one bucket.
    assert passed_count + failed_count + skipped_count == len(sorted_changes), (
        "validate() invariant violated: "
        f"passed({passed_count}) + failed({failed_count}) + "
        f"skipped({skipped_count}) != changes({len(sorted_changes)})"
    )

    return ValidationRunResult(
        results=results,
        approved_changes_considered=len(sorted_changes),
        passed_count=passed_count,
        failed_count=failed_count,
        skipped_count=skipped_count,
        results_by_rule=results_by_rule,
    )
