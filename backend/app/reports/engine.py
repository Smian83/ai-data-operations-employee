"""Module 20 — pure report engine.

build_report() is the only public function. It accepts all pipeline stage
rows as typed arguments (any may be None for a partial pipeline), performs
no I/O, no DB access, and returns the deterministic report_data dict that
is stored verbatim in ReportRun.report_data.

Architecture notes:
  - Every field that depends on a row that may be None is computed behind a
    null-guard: `row.field if row is not None else None`. No try/except
    swallows missing-row errors -- the calling convention is that None = stage
    not run, not None = stage ran and completed successfully.
  - Duration for each stage is (finished_at - started_at).total_seconds() from
    the corresponding TaskRun. None when either timestamp is absent.
  - pass_rate_pct: None when approved_changes_considered == 0 (no ZeroDivision).
  - Audit lineage section contains every upstream run ID, enabling an auditor
    to independently reconstruct the full pipeline chain from one report row.
  - Executive summary is generated from the aggregated report fields --
    no separate computation path.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.models.enums import REPORT_ENGINE_VERSION, REPORT_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Input dataclass -- passed by the handler so the engine stays pure
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ReportInput:
    """All resolved pipeline stage rows (any may be None for partial pipelines)
    plus org/task metadata needed for the job_summary and org_info sections.

    The handler resolves every row from the DB before calling build_report(),
    so the engine itself contains zero DB access and zero I/O.
    """
    # ---- Identity ----
    organization_id: uuid.UUID
    organization_name: str
    task_id: uuid.UUID
    task_name: str
    data_source_id: uuid.UUID
    data_source_name: str
    generated_by: str  # username/email of the user who triggered the REPORT run

    # ---- Pipeline stage TaskRuns (for duration computation) ----
    # Each entry: TaskRun ORM object or None if the stage was never run.
    sync_task_run: Any | None = None            # SYNC
    detect_task_run: Any | None = None          # DETECT
    remediate_task_run: Any | None = None       # REMEDIATE
    apply_task_run: Any | None = None           # APPLY_REMEDIATIONS
    validate_task_run: Any | None = None        # VALIDATE
    quality_ctrl_task_run: Any | None = None    # QUALITY_CTRL
    clean_export_task_run: Any | None = None    # CLEAN_EXPORT

    # ---- Pipeline stage result rows ----
    data_profile: Any | None = None             # DataProfile
    issue_detection_run: Any | None = None      # IssueDetectionRun
    remediation_run: Any | None = None          # RemediationRun
    applied_remediation_run: Any | None = None  # AppliedRemediationRun
    validation_run: Any | None = None           # ValidationRun
    quality_control_run: Any | None = None      # QualityControlRun
    clean_export: Any | None = None             # CleanExport (latest)
    export_run: Any | None = None               # ExportRun (EXPORT stage)

    # ---- Decision counts (from RemediationChangeDecision GROUP BY query) ----
    approved_decision_count: int = 0
    rejected_decision_count: int = 0
    pending_decision_count: int = 0

    # ---- Failed TaskRuns for the error/warning section ----
    failed_task_runs: list[Any] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _duration_seconds(task_run: Any | None) -> float | None:
    """Return (finished_at - started_at).total_seconds() or None."""
    if task_run is None:
        return None
    started = getattr(task_run, "started_at", None)
    finished = getattr(task_run, "finished_at", None)
    if started is None or finished is None:
        return None
    return (finished - started).total_seconds()


def _safe_rate(numerator: int | None, denominator: int | None) -> float | None:
    """Return numerator / denominator * 100 rounded to 2dp, or None on any
    missing / zero denominator."""
    if numerator is None or denominator is None or denominator == 0:
        return None
    return round(numerator / denominator * 100, 2)


def _pipeline_status(inp: ReportInput) -> str:
    """overall_status: 'complete' if QC + export done, 'partial' if some stages
    ran, 'no_data' if nothing ran."""
    if inp.data_profile is None:
        return "no_data"
    if inp.quality_control_run is not None:
        return "complete"
    return "partial"


def _pipeline_stages_completed(inp: ReportInput) -> list[str]:
    stages = []
    if inp.data_profile is not None:
        stages.append("sync")
    if inp.issue_detection_run is not None:
        stages.append("detect")
    if inp.remediation_run is not None:
        stages.append("remediate")
    if inp.applied_remediation_run is not None:
        stages.append("apply_remediations")
    if inp.validation_run is not None:
        stages.append("validate")
    if inp.quality_control_run is not None:
        stages.append("quality_ctrl")
    if inp.clean_export is not None:
        stages.append("clean_export")
    return stages


def _pipeline_stages_missing(inp: ReportInput) -> list[str]:
    all_stages = [
        "sync", "detect", "remediate", "apply_remediations",
        "validate", "quality_ctrl", "clean_export",
    ]
    completed = set(_pipeline_stages_completed(inp))
    return [s for s in all_stages if s not in completed]


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------

def build_report(inp: ReportInput) -> dict[str, Any]:
    """Build and return the full report_data dict from a ReportInput.

    Pure function: no DB access, no I/O, no side effects. Called by
    ReportHandler after all rows have been loaded. The returned dict is
    stored verbatim as ReportRun.report_data.

    All nullable fields are None (not absent) when the upstream stage was
    not run, so callers and clients always receive the same key structure
    regardless of pipeline completeness.
    """
    now_utc = datetime.now(timezone.utc).isoformat()

    # ---- Derived counts and rates ----
    dp = inp.data_profile
    idr = inp.issue_detection_run
    rem = inp.remediation_run
    arm = inp.applied_remediation_run
    val = inp.validation_run
    qcr = inp.quality_control_run
    cex = inp.clean_export

    total_issues: int | None = getattr(idr, "total_issues_found", None)
    approved: int = inp.approved_decision_count
    rejected: int = inp.rejected_decision_count
    pending: int = inp.pending_decision_count

    passed_count: int | None = getattr(val, "passed_count", None)
    failed_count: int | None = getattr(val, "failed_count", None)
    skipped_count: int | None = getattr(val, "skipped_count", None)
    approved_changes_considered: int | None = getattr(
        val, "approved_changes_considered", None
    )

    pass_rate_pct = _safe_rate(passed_count, approved_changes_considered)

    overall_score: float | None = getattr(qcr, "overall_score", None)
    release_recommendation: str | None = getattr(qcr, "release_recommendation", None)

    # ---- Dataset Health Score (0–100) ----
    # Computed as: if QC overall_score exists, use it; else None.
    dataset_health_score: float | None = overall_score

    # ---- Data improvement metrics ----
    post_stats: dict | None = getattr(qcr, "post_remediation_stats", None)
    issues_resolved: int | None = None
    if post_stats is not None:
        issues_resolved = post_stats.get("addressed_issue_count")

    # ---- Stage durations ----
    duration_sync = _duration_seconds(inp.sync_task_run)
    duration_detect = _duration_seconds(inp.detect_task_run)
    duration_remediate = _duration_seconds(inp.remediate_task_run)
    duration_apply = _duration_seconds(inp.apply_task_run)
    duration_validate = _duration_seconds(inp.validate_task_run)
    duration_qc = _duration_seconds(inp.quality_ctrl_task_run)
    duration_clean_export = _duration_seconds(inp.clean_export_task_run)

    defined_durations = [
        d for d in [
            duration_sync, duration_detect, duration_remediate,
            duration_apply, duration_validate, duration_qc, duration_clean_export,
        ]
        if d is not None
    ]
    total_pipeline_seconds: float | None = (
        round(sum(defined_durations), 3) if defined_durations else None
    )

    # ---- Export info ----
    export_run_status: str | None = getattr(inp.export_run, "status", None)
    export_run_id: uuid.UUID | None = getattr(inp.export_run, "id", None)
    clean_export_status: str | None = getattr(cex, "status", None)
    clean_export_format: str | None = getattr(cex, "format", None)
    clean_export_row_count: int | None = getattr(cex, "row_count", None)
    clean_export_col_count: int | None = getattr(cex, "column_count", None)
    clean_export_id: uuid.UUID | None = getattr(cex, "id", None)

    # ---- Applied changes ----
    applied_count: int | None = getattr(arm, "applied_change_count", None)
    skipped_applied: int | None = getattr(arm, "skipped_change_count", None)

    # ---- Source dataset ----
    source_col_names: list[str] = []
    if dp is not None and hasattr(dp, "column_profiles") and dp.column_profiles:
        # column_profiles is a list of dicts; extract column_name if present.
        for col in dp.column_profiles:
            if isinstance(col, dict) and "column_name" in col:
                source_col_names.append(col["column_name"])

    # ---- Audit lineage UUIDs (all as str for JSON serialization) ----
    def _id(obj: Any | None) -> str | None:
        if obj is None:
            return None
        val_id = getattr(obj, "id", None)
        return str(val_id) if val_id is not None else None

    # audit_lineage contains only IDs that are verifiably part of the same
    # pipeline chain anchored by quality_control_run_id.  export_run_id is
    # intentionally excluded: ExportRun is from the Module 9 MATCH→EXPORT
    # pipeline and has no FK to QualityControlRun, so it cannot be anchored
    # to the chain and would be misleading in an audit context.  It remains
    # in the export_result section as informational context.
    audit_lineage = {
        "data_profile_id": _id(dp),
        "issue_detection_run_id": _id(idr),
        "remediation_run_id": _id(rem),
        "applied_remediation_run_id": _id(arm),
        "validation_run_id": _id(val),
        "quality_control_run_id": _id(qcr),
        "clean_export_id": str(clean_export_id) if clean_export_id is not None else None,
    }

    # ---- Failed task run entries for errors section ----
    error_entries: list[dict[str, Any]] = []
    for tr in inp.failed_task_runs:
        error_entries.append({
            "task_type": str(getattr(tr, "task_type", "unknown")),
            "task_run_id": str(getattr(tr, "id", "")),
            "error_message": getattr(tr, "error_message", None),
        })

    # ---- Overall pipeline status ----
    pipeline_status = _pipeline_status(inp)
    stages_completed = _pipeline_stages_completed(inp)
    stages_missing = _pipeline_stages_missing(inp)

    # ---- Executive summary ----
    # Condensed top-level view -- all key metrics in one flat section.
    executive_summary: dict[str, Any] = {
        "dataset_health_score": dataset_health_score,
        "rows_processed": getattr(dp, "row_count", None),
        "columns_processed": getattr(dp, "column_count", None),
        "issues_found": total_issues,
        "approved_changes": approved if approved > 0 or rem is not None else None,
        "rejected_changes": rejected if rejected > 0 or rem is not None else None,
        "applied_changes": applied_count,
        "validation_pass_rate_pct": pass_rate_pct,
        "quality_score": overall_score,
        "release_recommendation": release_recommendation,
        "overall_pipeline_status": pipeline_status,
    }

    # ---- Assemble full report ----
    report: dict[str, Any] = {
        # ---- Versioning ----
        "report_schema_version": REPORT_SCHEMA_VERSION,
        "report_engine_version": REPORT_ENGINE_VERSION,

        # ---- Organization metadata ----
        "organization_id": str(inp.organization_id),
        "organization_name": inp.organization_name,
        "generated_by": inp.generated_by,
        "generated_at": now_utc,

        # ---- Executive summary ----
        "executive_summary": executive_summary,

        # ---- Job summary ----
        "job_summary": {
            "task_id": str(inp.task_id),
            "task_name": inp.task_name,
            "data_source_id": str(inp.data_source_id),
            "data_source_name": inp.data_source_name,
            "pipeline_stages_completed": stages_completed,
            "pipeline_stages_missing": stages_missing,
            "overall_status": pipeline_status,
        },

        # ---- Source dataset ----
        "source_dataset": {
            "filename": getattr(dp, "source_filename", None),
            "size_bytes": getattr(dp, "source_size_bytes", None),
            "row_count": getattr(dp, "row_count", None),
            "column_count": getattr(dp, "column_count", None),
            "column_names": source_col_names,
            "duplicate_row_count": getattr(dp, "duplicate_row_count", None),
            "missing_value_total": getattr(dp, "missing_value_total", None),
            "sha256": getattr(dp, "source_sha256", None),
            "data_profile_id": _id(dp),
        },

        # ---- Issues detected ----
        "issues_detected": {
            "total_found": total_issues,
            "by_severity": dict(getattr(idr, "issues_by_severity", None) or {}),
            "by_type": dict(getattr(idr, "issues_by_type", None) or {}),
            "issue_detection_run_id": _id(idr),
        },

        # ---- Cleaning actions proposed ----
        "cleaning_actions_proposed": {
            "total_changes_proposed": getattr(rem, "total_changes_count", None),
            "issues_skipped": getattr(rem, "issues_skipped_count", None),
            "by_action": dict(getattr(rem, "changes_by_action", None) or {}),
            "remediation_run_id": _id(rem),
        },

        # ---- Approval decisions ----
        "decisions": {
            "total_changes": (
                approved + rejected + pending
                if (rem is not None or approved + rejected + pending > 0)
                else None
            ),
            "approved": approved if (rem is not None or approved > 0) else None,
            "rejected": rejected if (rem is not None or rejected > 0) else None,
            "pending_decision": pending if (rem is not None or pending > 0) else None,
        },

        # ---- Changes applied ----
        "changes_applied": {
            "applied": applied_count,
            "skipped_unapplicable": skipped_applied,
            "applied_remediation_run_id": _id(arm),
        },

        # ---- Validation results ----
        "validation_results": {
            "total_evaluated": approved_changes_considered,
            "passed": passed_count,
            "failed": failed_count,
            "skipped": skipped_count,
            "pass_rate_pct": pass_rate_pct,
            "validation_run_id": _id(val),
        },

        # ---- Quality control ----
        "quality_control": {
            "overall_score": overall_score,
            "release_recommendation": release_recommendation,
            "blocking_findings": getattr(qcr, "blocking_count", None),
            "warning_findings": getattr(qcr, "warning_count", None),
            "info_findings": getattr(qcr, "info_count", None),
            "category_statuses": dict(
                getattr(qcr, "category_statuses", None) or {}
            ),
            "quality_control_run_id": _id(qcr),
        },

        # ---- Export result ----
        "export_result": {
            "export_run_status": export_run_status,
            "export_run_id": str(export_run_id) if export_run_id else None,
            "clean_export_status": clean_export_status,
            "clean_export_format": clean_export_format,
            "clean_export_row_count": clean_export_row_count,
            "clean_export_column_count": clean_export_col_count,
            "clean_export_id": str(clean_export_id) if clean_export_id else None,
        },

        # ---- Quality improvement metrics ----
        "quality_improvement": {
            "issues_before": total_issues,
            "approved_changes": approved if rem is not None else None,
            "changes_successfully_validated": passed_count,
            "validation_pass_rate_pct": pass_rate_pct,
            "issues_resolved_by_approved_changes": issues_resolved,
            "quality_score": overall_score,
            "post_remediation_row_count": (
                post_stats.get("effective_row_count")
                if post_stats is not None else None
            ),
        },

        # ---- Processing durations ----
        "processing_duration": {
            "total_pipeline_seconds": total_pipeline_seconds,
            "by_stage": {
                "sync": duration_sync,
                "detect": duration_detect,
                "remediate": duration_remediate,
                "apply_remediations": duration_apply,
                "validate": duration_validate,
                "quality_ctrl": duration_qc,
                "clean_export": duration_clean_export,
            },
        },

        # ---- Errors and warnings ----
        "errors_and_warnings": {
            "failed_task_runs": error_entries,
            "report_warnings": _collect_warnings(inp),
        },

        # ---- Audit lineage ----
        "audit_lineage": audit_lineage,
    }

    return report


def _collect_warnings(inp: ReportInput) -> list[str]:
    """Collect non-fatal warnings about the pipeline state."""
    warnings: list[str] = []
    if inp.data_profile is not None and inp.issue_detection_run is None:
        warnings.append(
            "Issue detection has not been run for this dataset; "
            "detected issue counts are unavailable."
        )
    if inp.issue_detection_run is not None and inp.remediation_run is None:
        warnings.append(
            "Remediation has not been run; cleaning action counts are unavailable."
        )
    if inp.remediation_run is not None and inp.applied_remediation_run is None:
        warnings.append(
            "No approved remediation changes have been applied yet."
        )
    if inp.applied_remediation_run is not None and inp.validation_run is None:
        warnings.append(
            "Validation has not been run after applying remediations."
        )
    if inp.validation_run is not None and inp.quality_control_run is None:
        warnings.append(
            "Quality control has not been run; no release recommendation is available."
        )
    if inp.quality_control_run is not None and inp.clean_export is None:
        warnings.append(
            "No clean export has been produced yet for this pipeline chain."
        )
    if inp.quality_control_run is not None:
        rec = getattr(inp.quality_control_run, "release_recommendation", None)
        if rec == "FAIL":
            warnings.append(
                "Quality control recommendation is FAIL; "
                "dataset does not meet release criteria."
            )
    return warnings
