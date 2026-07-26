"""The pure detection engine: detect_issues(dataset, limits) -> DetectionResult.

No I/O, no randomness, no database/session access anywhere in this module
-- exactly the app.cleaning.engine.clean / app.standardization.engine
precedent. All file/DB I/O happens only in
app.worker.handlers.issue_detection.IssueDetectionHandler.

Deterministic execution: DETECTION_RULES is iterated in a fixed order,
each rule's findings are collected in the order it yields them, and the
combined list is then sorted by (row_number, column_name, issue_type) so
the same dataset+config always produces byte-identical output regardless
of dict/set iteration order inside any individual rule -- this is what
the repeatability tests in Phase 2 verify.

Read-only guarantee: this function never mutates dataset.rows/headers/
structural_issues/column_rules, never writes to disk, and returns a new
DetectionResult built entirely from Finding objects yielded by rules --
see app.detection package docstring for the full read-only contract."""
from __future__ import annotations

from app.detection.registry import DETECTION_RULES
from app.detection.types import DetectionDataset, DetectionLimits, DetectionResult, Finding

# Same "small string, bumped whenever detection logic changes in a way
# that could change findings for identical input" convention as
# app.cleaning.engine.CLEANING_ENGINE_VERSION / app.standardization.
# engine.STANDARDIZATION_ENGINE_VERSION -- persisted verbatim onto every
# IssueDetectionRun.detection_engine_version by
# app.worker.handlers.issue_detection.
DETECTION_ENGINE_VERSION = "1.0"


def _sort_key(finding: Finding) -> tuple[int, str, str]:
    return (finding.row_number, finding.column_name or "", finding.issue_type)


def detect_issues(dataset: DetectionDataset, limits: DetectionLimits) -> DetectionResult:
    all_findings: list[Finding] = []
    for rule in DETECTION_RULES:
        all_findings.extend(rule.detect(dataset))

    all_findings.sort(key=_sort_key)

    total_issues_found = len(all_findings)
    persisted_findings = all_findings[: limits.max_persisted_issues]
    persisted_issue_count = len(persisted_findings)

    # Summary totals are always computed from the full, uncapped list --
    # persisting fewer findings than were found must never understate
    # issues_by_severity/issues_by_type (approved correction #7).
    issues_by_severity: dict[str, int] = {}
    issues_by_type: dict[str, int] = {}
    for finding in all_findings:
        issues_by_severity[finding.severity] = issues_by_severity.get(finding.severity, 0) + 1
        issues_by_type[finding.issue_type] = issues_by_type.get(finding.issue_type, 0) + 1

    return DetectionResult(
        findings=persisted_findings,
        total_issues_found=total_issues_found,
        persisted_issue_count=persisted_issue_count,
        issues_by_severity=issues_by_severity,
        issues_by_type=issues_by_type,
        rows_scanned=len(dataset.rows),
        columns_scanned=len(dataset.headers),
    )
