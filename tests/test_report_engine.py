"""Module 20: unit tests for the pure report engine (build_report).

No DB access, no I/O. All tests call build_report() directly with
ReportInput objects populated from simple mock namespaces. The engine
is pure: same input always produces the same output structure.

Scenarios covered (32):
  versioning:
    1.  report_schema_version and report_engine_version present in output
    2.  version constants match REPORT_ENGINE_VERSION / REPORT_SCHEMA_VERSION
  org metadata:
    3.  organization_id / organization_name / generated_by in output
    4.  generated_at is a valid ISO 8601 UTC string
  executive_summary (full pipeline):
    5.  dataset_health_score = qcr.overall_score
    6.  rows_processed = dp.row_count
    7.  columns_processed = dp.column_count
    8.  issues_found = idr.total_issues_found
    9.  approved_changes = approved_decision_count
   10.  rejected_changes = rejected_decision_count
   11.  applied_changes = arm.applied_change_count
   12.  validation_pass_rate_pct = safe_rate(passed_count, approved_changes_considered)
   13.  quality_score = qcr.overall_score
   14.  release_recommendation = qcr.release_recommendation
   15.  overall_pipeline_status = 'complete' when qcr present
  pipeline_status:
   16.  'no_data' when data_profile is None
   17.  'partial' when data_profile set but qcr is None
   18.  'complete' when qcr is set
  stages_completed / stages_missing:
   19.  all 7 stages listed as completed when all present
   20.  empty completed + all 7 missing when nothing present
  source_dataset:
   21.  column_names extracted from column_profiles list of dicts
   22.  sha256 / size_bytes / duplicate_row_count populated
  audit_lineage:
   23.  all 8 lineage IDs present as strings (or None)
   24.  all IDs match the respective ORM objects
  processing_duration:
   25.  total_pipeline_seconds is sum of non-None stage durations
   26.  stage duration is None when either timestamp absent
   27.  total_pipeline_seconds is None when no stage has timestamps
  errors_and_warnings:
   28.  failed_task_runs appear in errors_and_warnings.failed_task_runs
   29.  warnings emitted when detect run missing after sync
   30.  warnings emitted when remediate missing after detect
   31.  FAIL recommendation emits a warning
  edge cases:
   32.  _safe_rate returns None when denominator is 0 or None
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest

from app.models.enums import REPORT_ENGINE_VERSION, REPORT_SCHEMA_VERSION
from app.reports.engine import (
    ReportInput,
    _collect_warnings,
    _duration_seconds,
    _pipeline_stages_completed,
    _pipeline_stages_missing,
    _pipeline_status,
    _safe_rate,
    build_report,
)


# ---------------------------------------------------------------------------
# Helpers: lightweight mock objects for pipeline stage rows
# ---------------------------------------------------------------------------

def _task_run(*, started_offset_s: float = 10.0, duration_s: float = 2.0) -> Any:
    """Mock TaskRun with started_at / finished_at."""
    now = datetime.now(timezone.utc)
    return SimpleNamespace(
        id=uuid.uuid4(),
        started_at=now - timedelta(seconds=started_offset_s + duration_s),
        finished_at=now - timedelta(seconds=started_offset_s),
        status="success",
    )


def _task_run_no_times() -> Any:
    return SimpleNamespace(id=uuid.uuid4(), started_at=None, finished_at=None)


def _data_profile(*, row_count: int = 100, column_count: int = 5) -> Any:
    dp_id = uuid.uuid4()
    return SimpleNamespace(
        id=dp_id,
        row_count=row_count,
        column_count=column_count,
        source_filename="data.csv",
        source_size_bytes=4096,
        source_sha256="a" * 64,
        duplicate_row_count=2,
        missing_value_total=3,
        column_profiles=[
            {"column_name": "id", "type": "integer"},
            {"column_name": "name", "type": "string"},
        ],
        task_run_id=uuid.uuid4(),
    )


def _issue_detection_run(*, total: int = 5) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        total_issues_found=total,
        issues_by_severity={"LOW": 3, "HIGH": 2},
        issues_by_type={"missing_value": 3, "whitespace": 2},
        task_run_id=uuid.uuid4(),
    )


def _remediation_run(*, total_changes: int = 4) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        total_changes_count=total_changes,
        issues_skipped_count=1,
        changes_by_action={"trim_whitespace": 4},
        task_run_id=uuid.uuid4(),
    )


def _applied_remediation_run(*, applied: int = 4, skipped: int = 0) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        applied_change_count=applied,
        skipped_change_count=skipped,
        task_run_id=uuid.uuid4(),
    )


def _validation_run(
    *,
    passed: int = 4,
    failed: int = 0,
    skipped: int = 0,
    considered: int = 4,
    arm_id: uuid.UUID | None = None,
) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        passed_count=passed,
        failed_count=failed,
        skipped_count=skipped,
        approved_changes_considered=considered,
        applied_remediation_run_id=arm_id,
        task_run_id=uuid.uuid4(),
    )


def _quality_control_run(
    *,
    score: float = 88.5,
    recommendation: str = "PASS",
    dp_id: uuid.UUID | None = None,
    idr_id: uuid.UUID | None = None,
    rem_id: uuid.UUID | None = None,
    val_id: uuid.UUID | None = None,
) -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        overall_score=score,
        release_recommendation=recommendation,
        blocking_count=0,
        warning_count=1,
        info_count=2,
        category_statuses={"completeness": "passed", "validity": "passed"},
        post_remediation_stats={
            "effective_row_count": 98,
            "addressed_issue_count": 4,
        },
        data_profile_id=dp_id,
        issue_detection_run_id=idr_id,
        remediation_run_id=rem_id,
        validation_run_id=val_id,
        task_run_id=uuid.uuid4(),
    )


def _export_run() -> Any:
    return SimpleNamespace(id=uuid.uuid4(), status="approved")


def _clean_export() -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        status="completed",
        format="csv",
        row_count=98,
        column_count=5,
    )


def _failed_task_run(error_message: str = "boom") -> Any:
    return SimpleNamespace(
        id=uuid.uuid4(),
        task_type="detect",
        error_message=error_message,
    )


def _full_input(
    *,
    approved: int = 4,
    rejected: int = 1,
    pending: int = 0,
    failed_runs: list | None = None,
) -> ReportInput:
    """Build a ReportInput with all stages populated."""
    dp = _data_profile()
    idr = _issue_detection_run()
    rem = _remediation_run()
    arm = _applied_remediation_run()
    val = _validation_run(arm_id=arm.id, considered=approved + rejected)
    qcr = _quality_control_run(
        dp_id=dp.id, idr_id=idr.id, rem_id=rem.id, val_id=val.id
    )
    cex = _clean_export()
    exp = _export_run()

    return ReportInput(
        organization_id=uuid.uuid4(),
        organization_name="Acme Corp",
        task_id=uuid.uuid4(),
        task_name="Full pipeline task",
        data_source_id=uuid.uuid4(),
        data_source_name="customers.csv",
        generated_by="user@example.com",
        sync_task_run=_task_run(duration_s=3.0),
        detect_task_run=_task_run(duration_s=1.5),
        remediate_task_run=_task_run(duration_s=0.8),
        apply_task_run=_task_run(duration_s=1.1),
        validate_task_run=_task_run(duration_s=0.5),
        quality_ctrl_task_run=_task_run(duration_s=0.4),
        clean_export_task_run=_task_run(duration_s=0.3),
        data_profile=dp,
        issue_detection_run=idr,
        remediation_run=rem,
        applied_remediation_run=arm,
        validation_run=val,
        quality_control_run=qcr,
        clean_export=cex,
        export_run=exp,
        approved_decision_count=approved,
        rejected_decision_count=rejected,
        pending_decision_count=pending,
        failed_task_runs=failed_runs or [],
    )


# ---------------------------------------------------------------------------
# 1-2: Versioning
# ---------------------------------------------------------------------------

def test_report_schema_version_present() -> None:
    r = build_report(_full_input())
    assert r["report_schema_version"] == REPORT_SCHEMA_VERSION


def test_report_engine_version_present() -> None:
    r = build_report(_full_input())
    assert r["report_engine_version"] == REPORT_ENGINE_VERSION


# ---------------------------------------------------------------------------
# 3-4: Org metadata
# ---------------------------------------------------------------------------

def test_org_metadata_fields_present() -> None:
    inp = _full_input()
    r = build_report(inp)
    assert r["organization_id"] == str(inp.organization_id)
    assert r["organization_name"] == "Acme Corp"
    assert r["generated_by"] == "user@example.com"


def test_generated_at_is_iso_utc_string() -> None:
    r = build_report(_full_input())
    generated_at = r["generated_at"]
    # Must parse as ISO datetime without error.
    dt = datetime.fromisoformat(generated_at)
    # SQLite-safe: just check it has a UTC offset or ends with +00:00 / Z.
    assert dt.tzinfo is not None or generated_at.endswith("+00:00") or generated_at.endswith("Z")


# ---------------------------------------------------------------------------
# 5-15: Executive summary (full pipeline)
# ---------------------------------------------------------------------------

def test_exec_summary_dataset_health_score() -> None:
    inp = _full_input()
    r = build_report(inp)
    assert r["executive_summary"]["dataset_health_score"] == pytest.approx(
        inp.quality_control_run.overall_score
    )


def test_exec_summary_rows_processed() -> None:
    inp = _full_input()
    r = build_report(inp)
    assert r["executive_summary"]["rows_processed"] == inp.data_profile.row_count


def test_exec_summary_columns_processed() -> None:
    inp = _full_input()
    r = build_report(inp)
    assert r["executive_summary"]["columns_processed"] == inp.data_profile.column_count


def test_exec_summary_issues_found() -> None:
    inp = _full_input()
    r = build_report(inp)
    assert r["executive_summary"]["issues_found"] == inp.issue_detection_run.total_issues_found


def test_exec_summary_approved_changes() -> None:
    inp = _full_input(approved=3)
    r = build_report(inp)
    assert r["executive_summary"]["approved_changes"] == 3


def test_exec_summary_rejected_changes() -> None:
    inp = _full_input(rejected=2)
    r = build_report(inp)
    assert r["executive_summary"]["rejected_changes"] == 2


def test_exec_summary_applied_changes() -> None:
    inp = _full_input()
    r = build_report(inp)
    assert r["executive_summary"]["applied_changes"] == inp.applied_remediation_run.applied_change_count


def test_exec_summary_validation_pass_rate() -> None:
    # 4 passed / 5 considered = 80.0 %
    inp = _full_input(approved=5)
    # rebuild val with 5 considered and 4 passed
    dp = inp.data_profile
    idr = inp.issue_detection_run
    rem = inp.remediation_run
    arm = _applied_remediation_run(applied=4)
    val = _validation_run(passed=4, considered=5, arm_id=arm.id)
    qcr = _quality_control_run(dp_id=dp.id, idr_id=idr.id, rem_id=rem.id, val_id=val.id)
    inp2 = ReportInput(
        organization_id=inp.organization_id,
        organization_name=inp.organization_name,
        task_id=inp.task_id,
        task_name=inp.task_name,
        data_source_id=inp.data_source_id,
        data_source_name=inp.data_source_name,
        generated_by=inp.generated_by,
        data_profile=dp,
        issue_detection_run=idr,
        remediation_run=rem,
        applied_remediation_run=arm,
        validation_run=val,
        quality_control_run=qcr,
        approved_decision_count=5,
    )
    r = build_report(inp2)
    assert r["executive_summary"]["validation_pass_rate_pct"] == pytest.approx(80.0)


def test_exec_summary_quality_score() -> None:
    inp = _full_input()
    r = build_report(inp)
    assert r["executive_summary"]["quality_score"] == pytest.approx(
        inp.quality_control_run.overall_score
    )


def test_exec_summary_release_recommendation() -> None:
    inp = _full_input()
    r = build_report(inp)
    assert r["executive_summary"]["release_recommendation"] == "PASS"


def test_exec_summary_overall_pipeline_status_complete() -> None:
    inp = _full_input()
    r = build_report(inp)
    assert r["executive_summary"]["overall_pipeline_status"] == "complete"


# ---------------------------------------------------------------------------
# 16-18: Pipeline status
# ---------------------------------------------------------------------------

def test_pipeline_status_no_data() -> None:
    inp = ReportInput(
        organization_id=uuid.uuid4(),
        organization_name="X",
        task_id=uuid.uuid4(),
        task_name="T",
        data_source_id=uuid.uuid4(),
        data_source_name="S",
        generated_by="u",
    )
    r = build_report(inp)
    assert r["job_summary"]["overall_status"] == "no_data"
    assert r["executive_summary"]["overall_pipeline_status"] == "no_data"


def test_pipeline_status_partial() -> None:
    inp = ReportInput(
        organization_id=uuid.uuid4(),
        organization_name="X",
        task_id=uuid.uuid4(),
        task_name="T",
        data_source_id=uuid.uuid4(),
        data_source_name="S",
        generated_by="u",
        data_profile=_data_profile(),
    )
    r = build_report(inp)
    assert r["job_summary"]["overall_status"] == "partial"


def test_pipeline_status_complete() -> None:
    r = build_report(_full_input())
    assert r["job_summary"]["overall_status"] == "complete"


# ---------------------------------------------------------------------------
# 19-20: Stages completed / missing
# ---------------------------------------------------------------------------

def test_all_stages_completed_when_all_present() -> None:
    r = build_report(_full_input())
    completed = set(r["job_summary"]["pipeline_stages_completed"])
    assert completed == {
        "sync", "detect", "remediate", "apply_remediations",
        "validate", "quality_ctrl", "clean_export",
    }
    assert r["job_summary"]["pipeline_stages_missing"] == []


def test_all_stages_missing_when_nothing_present() -> None:
    inp = ReportInput(
        organization_id=uuid.uuid4(),
        organization_name="X",
        task_id=uuid.uuid4(),
        task_name="T",
        data_source_id=uuid.uuid4(),
        data_source_name="S",
        generated_by="u",
    )
    r = build_report(inp)
    assert r["job_summary"]["pipeline_stages_completed"] == []
    assert set(r["job_summary"]["pipeline_stages_missing"]) == {
        "sync", "detect", "remediate", "apply_remediations",
        "validate", "quality_ctrl", "clean_export",
    }


# ---------------------------------------------------------------------------
# 21-22: Source dataset
# ---------------------------------------------------------------------------

def test_source_dataset_column_names_extracted() -> None:
    dp = _data_profile()
    inp = ReportInput(
        organization_id=uuid.uuid4(),
        organization_name="X",
        task_id=uuid.uuid4(),
        task_name="T",
        data_source_id=uuid.uuid4(),
        data_source_name="S",
        generated_by="u",
        data_profile=dp,
    )
    r = build_report(inp)
    assert r["source_dataset"]["column_names"] == ["id", "name"]


def test_source_dataset_sha256_and_size() -> None:
    dp = _data_profile()
    inp = ReportInput(
        organization_id=uuid.uuid4(),
        organization_name="X",
        task_id=uuid.uuid4(),
        task_name="T",
        data_source_id=uuid.uuid4(),
        data_source_name="S",
        generated_by="u",
        data_profile=dp,
    )
    r = build_report(inp)
    assert r["source_dataset"]["sha256"] == "a" * 64
    assert r["source_dataset"]["size_bytes"] == 4096
    assert r["source_dataset"]["duplicate_row_count"] == 2


# ---------------------------------------------------------------------------
# 23-24: Audit lineage
# ---------------------------------------------------------------------------

def test_audit_lineage_all_keys_present() -> None:
    r = build_report(_full_input())
    al = r["audit_lineage"]
    expected_keys = {
        "data_profile_id",
        "issue_detection_run_id",
        "remediation_run_id",
        "applied_remediation_run_id",
        "validation_run_id",
        "quality_control_run_id",
        "clean_export_id",
    }
    assert set(al.keys()) == expected_keys


def test_audit_lineage_ids_match_objects() -> None:
    inp = _full_input()
    r = build_report(inp)
    al = r["audit_lineage"]
    assert al["data_profile_id"] == str(inp.data_profile.id)
    assert al["issue_detection_run_id"] == str(inp.issue_detection_run.id)
    assert al["remediation_run_id"] == str(inp.remediation_run.id)
    assert al["applied_remediation_run_id"] == str(inp.applied_remediation_run.id)
    assert al["validation_run_id"] == str(inp.validation_run.id)
    assert al["quality_control_run_id"] == str(inp.quality_control_run.id)
    assert al["clean_export_id"] == str(inp.clean_export.id)


# ---------------------------------------------------------------------------
# 25-27: Processing duration
# ---------------------------------------------------------------------------

def test_total_pipeline_seconds_is_sum_of_non_none_stages() -> None:
    # All 7 stages have 2.0s duration each → total = 14.0s.
    tr = _task_run(duration_s=2.0)
    inp = _full_input()
    inp2 = ReportInput(
        organization_id=inp.organization_id,
        organization_name=inp.organization_name,
        task_id=inp.task_id,
        task_name=inp.task_name,
        data_source_id=inp.data_source_id,
        data_source_name=inp.data_source_name,
        generated_by=inp.generated_by,
        sync_task_run=_task_run(duration_s=2.0),
        detect_task_run=_task_run(duration_s=2.0),
        remediate_task_run=_task_run(duration_s=2.0),
        apply_task_run=_task_run(duration_s=2.0),
        validate_task_run=_task_run(duration_s=2.0),
        quality_ctrl_task_run=_task_run(duration_s=2.0),
        clean_export_task_run=_task_run(duration_s=2.0),
        data_profile=inp.data_profile,
        quality_control_run=inp.quality_control_run,
    )
    r = build_report(inp2)
    assert r["processing_duration"]["total_pipeline_seconds"] == pytest.approx(14.0, abs=0.1)


def test_stage_duration_is_none_when_timestamps_absent() -> None:
    assert _duration_seconds(_task_run_no_times()) is None
    assert _duration_seconds(None) is None


def test_total_pipeline_seconds_none_when_no_timestamps() -> None:
    inp = ReportInput(
        organization_id=uuid.uuid4(),
        organization_name="X",
        task_id=uuid.uuid4(),
        task_name="T",
        data_source_id=uuid.uuid4(),
        data_source_name="S",
        generated_by="u",
        sync_task_run=_task_run_no_times(),
    )
    r = build_report(inp)
    assert r["processing_duration"]["total_pipeline_seconds"] is None


# ---------------------------------------------------------------------------
# 28-31: Errors and warnings
# ---------------------------------------------------------------------------

def test_failed_task_runs_appear_in_errors_section() -> None:
    ftrs = [_failed_task_run("timeout"), _failed_task_run("connection refused")]
    inp = _full_input(failed_runs=ftrs)
    r = build_report(inp)
    errs = r["errors_and_warnings"]["failed_task_runs"]
    assert len(errs) == 2
    error_msgs = [e["error_message"] for e in errs]
    assert "timeout" in error_msgs
    assert "connection refused" in error_msgs


def test_warning_when_detect_missing_after_sync() -> None:
    inp = ReportInput(
        organization_id=uuid.uuid4(),
        organization_name="X",
        task_id=uuid.uuid4(),
        task_name="T",
        data_source_id=uuid.uuid4(),
        data_source_name="S",
        generated_by="u",
        data_profile=_data_profile(),
        issue_detection_run=None,
    )
    warnings = _collect_warnings(inp)
    assert any("Issue detection" in w for w in warnings)


def test_warning_when_remediate_missing_after_detect() -> None:
    inp = ReportInput(
        organization_id=uuid.uuid4(),
        organization_name="X",
        task_id=uuid.uuid4(),
        task_name="T",
        data_source_id=uuid.uuid4(),
        data_source_name="S",
        generated_by="u",
        data_profile=_data_profile(),
        issue_detection_run=_issue_detection_run(),
        remediation_run=None,
    )
    warnings = _collect_warnings(inp)
    assert any("Remediation" in w for w in warnings)


def test_fail_recommendation_emits_warning() -> None:
    qcr = _quality_control_run(recommendation="FAIL")
    inp = ReportInput(
        organization_id=uuid.uuid4(),
        organization_name="X",
        task_id=uuid.uuid4(),
        task_name="T",
        data_source_id=uuid.uuid4(),
        data_source_name="S",
        generated_by="u",
        data_profile=_data_profile(),
        quality_control_run=qcr,
    )
    warnings = _collect_warnings(inp)
    assert any("FAIL" in w for w in warnings)


# ---------------------------------------------------------------------------
# 32: _safe_rate edge cases
# ---------------------------------------------------------------------------

def test_safe_rate_zero_denominator_returns_none() -> None:
    assert _safe_rate(0, 0) is None
    assert _safe_rate(5, 0) is None


def test_safe_rate_none_operands_returns_none() -> None:
    assert _safe_rate(None, 10) is None
    assert _safe_rate(10, None) is None


def test_safe_rate_correct_computation() -> None:
    assert _safe_rate(3, 4) == pytest.approx(75.0)
    assert _safe_rate(0, 10) == pytest.approx(0.0)
    assert _safe_rate(10, 10) == pytest.approx(100.0)


# ---------------------------------------------------------------------------
# Additional structural checks
# ---------------------------------------------------------------------------

def test_all_top_level_sections_present() -> None:
    r = build_report(_full_input())
    required_keys = {
        "report_schema_version", "report_engine_version",
        "organization_id", "organization_name", "generated_by", "generated_at",
        "executive_summary", "job_summary", "source_dataset",
        "issues_detected", "cleaning_actions_proposed", "decisions",
        "changes_applied", "validation_results", "quality_control",
        "export_result", "quality_improvement", "processing_duration",
        "errors_and_warnings", "audit_lineage",
    }
    assert required_keys.issubset(set(r.keys()))


def test_build_report_partial_all_none_values_present() -> None:
    """Partial pipeline: all nullable fields present as None (not missing)."""
    inp = ReportInput(
        organization_id=uuid.uuid4(),
        organization_name="Test",
        task_id=uuid.uuid4(),
        task_name="T",
        data_source_id=uuid.uuid4(),
        data_source_name="S",
        generated_by="u",
    )
    r = build_report(inp)
    # Executive summary keys exist even if all None.
    eskeys = {
        "dataset_health_score", "rows_processed", "columns_processed",
        "issues_found", "applied_changes", "validation_pass_rate_pct",
        "quality_score", "release_recommendation",
    }
    for k in eskeys:
        assert k in r["executive_summary"], f"Missing key: {k}"
        assert r["executive_summary"][k] is None, f"Expected None for {k}"


def test_determinism() -> None:
    """build_report is deterministic for all fields except generated_at."""
    inp = _full_input()
    r1 = build_report(inp)
    r2 = build_report(inp)
    # Remove the timestamp before comparing.
    for r in (r1, r2):
        r.pop("generated_at")
    assert r1 == r2
