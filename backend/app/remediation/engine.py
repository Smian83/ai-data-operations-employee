"""The pure remediation engine: remediate(dataset, issues, column_config,
dataset_config, limits) -> RemediationResult.

No I/O, no randomness, no database/session access anywhere in this
module -- exactly the app.detection.engine.detect_issues /
app.cleaning.engine.clean precedent. All file/DB I/O happens only in a
later phase's RemediationHandler.

Deterministic execution: issues are processed in a fixed sort order
(row_number, column_name, issue_type -- identical key shape to
app.detection.engine._sort_key) regardless of the order the caller passed
them in, so the same issue set + column_config + dataset_config always
produces byte-identical output (changes AND skipped, both independently
sorted the same way) -- this is what tests/test_remediation_repeatability.py
verifies. Every considered Issue ends up in exactly one of
RemediationResult.changes / RemediationResult.skipped, never both, never
neither (mirrors ck_remediation_runs_counts_reconcile at the database
layer).

Persisted-change ceiling: settings.remediation_max_persisted_changes
(RemediationLimits.max_persisted_changes) is a defensive ceiling only --
per the Module 15 design doc Section 3, it is "not expected to bind in
practice" since Module 15 has no persisted-vs-total-found capping concept
of its own (unlike Module 14/Module 6). If it is ever hit, the excess
changes (in the same deterministic order, i.e. the LAST ones by sort key)
are reclassified as skipped with skip_reasons.PERSISTED_CHANGE_LIMIT_
REACHED rather than silently dropped -- this is what keeps issues_
considered_count == total_changes_count + issues_skipped_count an
unconditional invariant even in this edge case, not just the common one.

Read-only guarantee: this function never mutates dataset.headers/rows or
any element of issues/column_config/dataset_config, never writes to disk,
and returns a new RemediationResult built entirely from RuleOutcome
objects returned by app.remediation.registry.REMEDIATION_RULES."""
from __future__ import annotations

from dataclasses import dataclass

from app.remediation.registry import get_rule_for_issue_type
from app.remediation.skip_reasons import (
    ISSUE_TYPE_NOT_REMEDIABLE,
    PERSISTED_CHANGE_LIMIT_REACHED,
    ROW_NOT_FOUND_IN_DATASET,
)
from app.remediation.types import (
    RemediationChangeProposal,
    RemediationColumnConfig,
    RemediationDataset,
    RemediationDatasetConfigInput,
    RemediationIssueInput,
    RemediationLimits,
    RemediationResult,
    SkippedIssue,
)

# Same "small string, bumped whenever remediation logic changes in a way
# that could change proposals for identical input" convention as
# app.detection.engine.DETECTION_ENGINE_VERSION / app.cleaning.engine.
# CLEANING_ENGINE_VERSION. RemediationRun.remediation_engine_version
# already exists as a column (Module 15 Phase 1) -- a later
# RemediationHandler imports this constant directly and persists it
# verbatim, the exact same "handler imports engine's version constant,
# never computes/guesses one" pattern IssueDetectionHandler already uses
# for DETECTION_ENGINE_VERSION (see app.worker.handlers.issue_detection).
REMEDIATION_ENGINE_VERSION = "1.0"


def _issue_sort_key(issue: RemediationIssueInput) -> tuple[int, str, str]:
    return (issue.row_number, issue.column_name or "", issue.issue_type)


def _skipped_sort_key(skipped: SkippedIssue) -> tuple[int, str, str]:
    return (skipped.row_number, skipped.column_name or "", skipped.issue_type)


@dataclass(frozen=True)
class _PendingChange:
    """Internal only -- pairs a proposal with the issue_type it came from,
    which RemediationChangeProposal itself does not carry (it mirrors
    RemediationChange, which has no issue_type column), so overflow
    handling can still build a fully-populated SkippedIssue if this
    change is later bumped by the persisted-change ceiling."""

    proposal: RemediationChangeProposal
    issue_type: str


def remediate(
    dataset: RemediationDataset,
    issues: list[RemediationIssueInput],
    column_config: dict[str, RemediationColumnConfig],
    dataset_config: RemediationDatasetConfigInput,
    limits: RemediationLimits,
) -> RemediationResult:
    sorted_issues = sorted(issues, key=_issue_sort_key)

    min_row_number = 2  # header is CSV row 1 -- see app.detection.rules' identical convention.
    max_row_number = len(dataset.rows) + 1

    pending_changes: list[_PendingChange] = []
    skipped: list[SkippedIssue] = []

    for issue in sorted_issues:
        rule = get_rule_for_issue_type(issue.issue_type)
        if rule is None:
            skipped.append(_skip(issue, ISSUE_TYPE_NOT_REMEDIABLE))
            continue

        if not (min_row_number <= issue.row_number <= max_row_number):
            skipped.append(_skip(issue, ROW_NOT_FOUND_IN_DATASET))
            continue

        resolved_column_config = (
            column_config.get(issue.column_name) if issue.column_name else None
        )
        outcome = rule.propose(issue, resolved_column_config, dataset_config)

        if not outcome.changed:
            skipped.append(_skip(issue, outcome.skip_reason))
            continue

        pending_changes.append(
            _PendingChange(
                proposal=RemediationChangeProposal(
                    source_issue_id=issue.issue_id,
                    row_number=issue.row_number,
                    column_name=issue.column_name,
                    action=rule.action,
                    original_value=issue.original_value,
                    proposed_value=outcome.proposed_value,
                    reason=outcome.reason,
                ),
                issue_type=issue.issue_type,
            )
        )

    kept, overflow = _apply_persisted_change_ceiling(pending_changes, limits)
    for pending in overflow:
        skipped.append(
            SkippedIssue(
                source_issue_id=pending.proposal.source_issue_id,
                row_number=pending.proposal.row_number,
                column_name=pending.proposal.column_name,
                issue_type=pending.issue_type,
                reason=PERSISTED_CHANGE_LIMIT_REACHED,
            )
        )
    skipped.sort(key=_skipped_sort_key)

    changes = [pending.proposal for pending in kept]
    changes_by_action: dict[str, int] = {}
    for proposal in changes:
        changes_by_action[proposal.action] = changes_by_action.get(proposal.action, 0) + 1

    return RemediationResult(
        changes=changes,
        skipped=skipped,
        issues_considered_count=len(issues),
        total_changes_count=len(changes),
        issues_skipped_count=len(skipped),
        changes_by_action=changes_by_action,
    )


def _skip(issue: RemediationIssueInput, reason: str) -> SkippedIssue:
    return SkippedIssue(
        source_issue_id=issue.issue_id,
        row_number=issue.row_number,
        column_name=issue.column_name,
        issue_type=issue.issue_type,
        reason=reason,
    )


def _apply_persisted_change_ceiling(
    pending_changes: list[_PendingChange], limits: RemediationLimits
) -> tuple[list[_PendingChange], list[_PendingChange]]:
    """kept is already in the same deterministic order pending_changes
    arrived in (issue sort order); overflow (if any) is always the tail
    -- the last changes by that same order, never an arbitrary subset."""
    if len(pending_changes) <= limits.max_persisted_changes:
        return pending_changes, []
    return (
        pending_changes[: limits.max_persisted_changes],
        pending_changes[limits.max_persisted_changes :],
    )
