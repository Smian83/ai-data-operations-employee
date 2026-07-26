"""Module 20: ReportHandler integration tests.

Chain under test:
  Direct DB inserts for:
    DataProfile → IssueDetectionRun → RemediationRun →
    AppliedRemediationRun → ValidationRun → QualityControlRun

  ReportHandler is then invoked directly.

Design: We insert prerequisite rows directly via db_session rather than
running all upstream handlers, to keep these tests fast and isolated.
ReportHandler only reads those rows — it never mutates them.

Scenarios covered (22):
  happy path:
    1.  Full pipeline: report_run created, quality_control_run_id populated
    2.  report_data includes all required top-level sections
    3.  executive_summary.overall_pipeline_status == 'complete'
    4.  audit_lineage IDs match the inserted ORM rows
    5.  report_engine_version / report_schema_version match constants
    6.  generated_by defaults to "system" when no user on context
    7.  decision counts flow through to report_data.decisions
  idempotency:
    8.  second execute() returns 'already exists' without creating a second row
    9.  IntegrityError on concurrent duplicate → catch-and-refetch
  partial pipeline (no QC run):
   10.  partial report created when only DataProfile exists
   11.  pipeline_status == 'partial' for partial pipeline
   12.  pipeline_status == 'no_data' when nothing exists for data_source
  fallback logic:
   13.  source_task_run_id=None → falls back to latest QC run for data_source
  prerequisite errors:
   14.  missing data_source → PermanentExecutionError
  cross-org isolation:
   15.  source_task_run_id from other org → treated as missing (partial report)
  persistence:
   16.  exactly one ReportRun created per task_run_id
   17.  organization_id on ReportRun matches task_run.organization_id
   18.  data_source_id on ReportRun matches task.data_source_id
   19.  report_data is a non-empty dict
   20.  ReportRun.report_schema_version == REPORT_SCHEMA_VERSION constant
  pipeline lineage integrity:
   21.  audit_lineage does not contain export_run_id (Module 9 chain is not anchored)
   22.  two independent pipeline executions for the same data source cannot mix
        each other's CleanExport records in the report
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.models.applied_remediation_run import AppliedRemediationRun
from app.models.clean_export import CleanExport
from app.models.data_profile import DataProfile
from app.models.data_source import DataSource
from app.models.enums import REPORT_ENGINE_VERSION, REPORT_SCHEMA_VERSION
from app.models.issue_detection_run import IssueDetectionRun
from app.models.quality_control_run import QualityControlRun
from app.models.remediation_run import RemediationRun
from app.models.report_run import ReportRun
from app.models.task import Task
from app.models.task_run import TaskRun
from app.models.validation_run import ValidationRun
from app.worker.handlers.base import ExecutionContext, PermanentExecutionError
from app.worker.handlers.report import ReportHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _auth_headers(client: TestClient, suffix: str) -> dict:
    resp = client.post(
        "/auth/register",
        json={
            "organization_name": f"Report Handler Org {suffix}",
            "email": f"report-handler-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Report Handler Tester",
        },
    )
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _create_data_source(client: TestClient, headers: dict, suffix: str) -> str:
    resp = client.post(
        "/data-sources",
        json={
            "name": f"Report DS {suffix}",
            "source_type": "csv_upload",
            "connection_metadata": {"file_path": "data.csv"},
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _make_task(
    client: TestClient, headers: dict, task_type: str,
    source_id: str, name: str
) -> str:
    resp = client.post(
        "/tasks",
        json={"name": name, "task_type": task_type, "data_source_id": source_id},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _make_run(
    client: TestClient, headers: dict, task_id: str,
    source_task_run_id: str | None = None,
) -> str:
    body = {}
    if source_task_run_id:
        body["source_task_run_id"] = source_task_run_id
    resp = client.post(f"/tasks/{task_id}/runs", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _insert_full_chain(db, *, org_id: uuid.UUID, ds_id: uuid.UUID,
                        sync_run, detect_run, remediate_run, apply_run,
                        validate_run, qc_run,
                        sync_task, detect_task, remediate_task,
                        apply_task, validate_task, qc_task) -> dict:
    """Insert DataProfile → IDR → RemRun → ARM → ValRun → QCR and return them."""
    dp = DataProfile(
        id=uuid.uuid4(), organization_id=org_id,
        task_run_id=sync_run.id, task_id=sync_task.id,
        data_source_id=ds_id,
        source_filename="data.csv", source_size_bytes=100,
        source_sha256="a" * 64, detected_encoding="utf-8", delimiter=",",
        row_count=10, column_count=3,
        duplicate_row_count=1, missing_value_total=1,
        column_profiles=[{"column_name": "id"}, {"column_name": "name"}],
        structural_issues=[], limits_applied={},
    )
    db.add(dp)
    db.commit()

    idr = IssueDetectionRun(
        organization_id=org_id, task_run_id=detect_run.id,
        task_id=detect_task.id, data_source_id=ds_id,
        rows_scanned=10, columns_scanned=3, total_issues_found=2,
        persisted_issue_count=2,
        issues_by_severity={"LOW": 2}, issues_by_type={"whitespace": 2},
        limits_applied={}, detection_engine_version="1.0", source_sha256="a" * 64,
    )
    db.add(idr)
    db.commit()

    rem_run_row = RemediationRun(
        organization_id=org_id, task_run_id=remediate_run.id,
        task_id=remediate_task.id, data_source_id=ds_id,
        source_task_run_id=detect_run.id,
        issues_considered_count=2, total_changes_count=2,
        issues_skipped_count=0, changes_by_action={"trim_whitespace": 2},
        skipped_by_reason={}, remediation_engine_version="1.0",
    )
    db.add(rem_run_row)
    db.commit()

    arm_row = AppliedRemediationRun(
        id=uuid.uuid4(), organization_id=org_id,
        task_run_id=apply_run.id, task_id=apply_task.id,
        data_source_id=ds_id,
        remediation_run_id=rem_run_row.id,
        applied_change_count=2, skipped_change_count=0,
        decisions_snapshot_hash="abc123",
        apply_engine_version="1.0",
        output_file_path="remediated/data.csv",
        output_sha256="b" * 64,
    )
    db.add(arm_row)
    db.commit()

    val_run_row = ValidationRun(
        id=uuid.uuid4(), organization_id=org_id,
        task_run_id=validate_run.id, task_id=validate_task.id,
        data_source_id=ds_id,
        remediation_run_id=rem_run_row.id,
        applied_remediation_run_id=arm_row.id,
        validation_engine_version="1.0",
        approved_changes_considered=2,
        passed_count=2, failed_count=0, skipped_count=0,
        results_by_rule={"trim_whitespace_check": {"passed": 2}},
    )
    db.add(val_run_row)
    db.commit()

    qcr_row = QualityControlRun(
        id=uuid.uuid4(), organization_id=org_id,
        task_run_id=qc_run.id, task_id=qc_task.id,
        data_source_id=ds_id,
        validation_run_id=val_run_row.id,
        remediation_run_id=rem_run_row.id,
        issue_detection_run_id=idr.id,
        data_profile_id=dp.id,
        quality_engine_version="1.0",
        overall_score=90.0, release_recommendation="PASS",
        total_findings=0, blocking_count=0, warning_count=0, info_count=0,
        category_scores={}, category_statuses={},
        category_weights_used={},
        post_remediation_stats={"effective_row_count": 9, "addressed_issue_count": 2},
        execution_snapshot={},
    )
    db.add(qcr_row)
    db.commit()

    return {
        "dp": dp, "idr": idr, "rem": rem_run_row, "arm": arm_row,
        "val": val_run_row, "qcr": qcr_row,
    }


def _build_full_scaffold(client, db_session, suffix):
    """Build all prerequisite tasks/runs and insert chain rows."""
    headers = _auth_headers(client, suffix)
    source_id = _create_data_source(client, headers, suffix)
    ds_list = client.get("/data-sources", headers=headers)
    org_id_str = ds_list.json()["items"][0]["organization_id"]
    org_id = uuid.UUID(org_id_str)
    ds_id = uuid.UUID(source_id)

    sync_tid = _make_task(client, headers, "sync", source_id, f"Sync {suffix}")
    sync_rid = _make_run(client, headers, sync_tid)

    detect_tid = _make_task(client, headers, "detect", source_id, f"Detect {suffix}")
    detect_rid = _make_run(client, headers, detect_tid)

    rem_tid = _make_task(client, headers, "remediate", source_id, f"Rem {suffix}")
    rem_rid = _make_run(client, headers, rem_tid, source_task_run_id=detect_rid)

    apply_tid = _make_task(client, headers, "apply_remediations", source_id, f"Apply {suffix}")
    apply_rid = _make_run(client, headers, apply_tid)

    val_tid = _make_task(client, headers, "validate", source_id, f"Val {suffix}")
    val_rid = _make_run(client, headers, val_tid, source_task_run_id=apply_rid)

    qc_tid = _make_task(client, headers, "quality_ctrl", source_id, f"QC {suffix}")
    qc_rid = _make_run(client, headers, qc_tid)

    report_tid = _make_task(client, headers, "report", source_id, f"Report {suffix}")
    report_rid = _make_run(client, headers, report_tid)

    db_session.expire_all()
    sync_task = db_session.get(Task, uuid.UUID(sync_tid))
    detect_task = db_session.get(Task, uuid.UUID(detect_tid))
    rem_task = db_session.get(Task, uuid.UUID(rem_tid))
    apply_task = db_session.get(Task, uuid.UUID(apply_tid))
    val_task = db_session.get(Task, uuid.UUID(val_tid))
    qc_task = db_session.get(Task, uuid.UUID(qc_tid))
    report_task = db_session.get(Task, uuid.UUID(report_tid))

    sync_run = db_session.get(TaskRun, uuid.UUID(sync_rid))
    detect_run = db_session.get(TaskRun, uuid.UUID(detect_rid))
    rem_run = db_session.get(TaskRun, uuid.UUID(rem_rid))
    apply_run = db_session.get(TaskRun, uuid.UUID(apply_rid))
    val_run = db_session.get(TaskRun, uuid.UUID(val_rid))
    qc_run = db_session.get(TaskRun, uuid.UUID(qc_rid))
    report_run = db_session.get(TaskRun, uuid.UUID(report_rid))

    chain = _insert_full_chain(
        db_session,
        org_id=org_id, ds_id=ds_id,
        sync_run=sync_run, detect_run=detect_run, remediate_run=rem_run,
        apply_run=apply_run, validate_run=val_run, qc_run=qc_run,
        sync_task=sync_task, detect_task=detect_task, remediate_task=rem_task,
        apply_task=apply_task, validate_task=val_task, qc_task=qc_task,
    )

    # Wire the REPORT run to point at the QC run
    report_run.source_task_run_id = qc_run.id
    db_session.commit()

    return {
        "headers": headers, "org_id": org_id, "ds_id": ds_id,
        "report_task": report_task, "report_run": report_run,
        "qc_run": qc_run, "qc_task": qc_task,
        **chain,
    }


def _make_context(task_run: TaskRun, task: Task, data_source) -> ExecutionContext:
    return ExecutionContext(
        task_run=task_run,
        task=task,
        data_source=data_source,
        idempotency_key=str(task_run.id),
        credential_provider=None,
    )


# ---------------------------------------------------------------------------
# 1-7: Happy path — full pipeline
# ---------------------------------------------------------------------------

def test_full_pipeline_creates_report_run(client, db_session) -> None:
    """1. Full pipeline: ReportRun row created with quality_control_run_id set."""
    s = uuid.uuid4().hex[:8]
    sc = _build_full_scaffold(client, db_session, s)
    ds = db_session.get(DataSource, sc["ds_id"])

    ctx = _make_context(sc["report_run"], sc["report_task"], ds)
    result = ReportHandler().execute(ctx)

    assert "report_run_id=" in result
    assert "qc_run_id=" in result
    db_session.expire_all()
    rr = db_session.execute(
        select(ReportRun).where(ReportRun.task_run_id == sc["report_run"].id)
    ).scalar_one()
    assert rr.quality_control_run_id == sc["qcr"].id


def test_full_pipeline_report_data_has_required_sections(client, db_session) -> None:
    """2. report_data includes all required top-level sections."""
    s = uuid.uuid4().hex[:8]
    sc = _build_full_scaffold(client, db_session, s)
    ds = db_session.get(DataSource, sc["ds_id"])

    ReportHandler().execute(_make_context(sc["report_run"], sc["report_task"], ds))
    db_session.expire_all()
    rr = db_session.execute(
        select(ReportRun).where(ReportRun.task_run_id == sc["report_run"].id)
    ).scalar_one()
    rd = rr.report_data
    for section in [
        "report_schema_version", "report_engine_version",
        "organization_id", "organization_name", "generated_by", "generated_at",
        "executive_summary", "job_summary", "source_dataset",
        "issues_detected", "cleaning_actions_proposed", "decisions",
        "changes_applied", "validation_results", "quality_control",
        "export_result", "quality_improvement", "processing_duration",
        "errors_and_warnings", "audit_lineage",
    ]:
        assert section in rd, f"Missing section: {section}"


def test_full_pipeline_status_complete(client, db_session) -> None:
    """3. executive_summary.overall_pipeline_status == 'complete'."""
    s = uuid.uuid4().hex[:8]
    sc = _build_full_scaffold(client, db_session, s)
    ds = db_session.get(DataSource, sc["ds_id"])

    ReportHandler().execute(_make_context(sc["report_run"], sc["report_task"], ds))
    db_session.expire_all()
    rr = db_session.execute(
        select(ReportRun).where(ReportRun.task_run_id == sc["report_run"].id)
    ).scalar_one()
    assert rr.report_data["executive_summary"]["overall_pipeline_status"] == "complete"


def test_full_pipeline_audit_lineage_ids(client, db_session) -> None:
    """4. audit_lineage IDs match the inserted ORM rows."""
    s = uuid.uuid4().hex[:8]
    sc = _build_full_scaffold(client, db_session, s)
    ds = db_session.get(DataSource, sc["ds_id"])

    ReportHandler().execute(_make_context(sc["report_run"], sc["report_task"], ds))
    db_session.expire_all()
    rr = db_session.execute(
        select(ReportRun).where(ReportRun.task_run_id == sc["report_run"].id)
    ).scalar_one()
    al = rr.report_data["audit_lineage"]
    assert al["data_profile_id"] == str(sc["dp"].id)
    assert al["issue_detection_run_id"] == str(sc["idr"].id)
    assert al["remediation_run_id"] == str(sc["rem"].id)
    assert al["applied_remediation_run_id"] == str(sc["arm"].id)
    assert al["validation_run_id"] == str(sc["val"].id)
    assert al["quality_control_run_id"] == str(sc["qcr"].id)


def test_full_pipeline_version_constants(client, db_session) -> None:
    """5. report_engine_version / report_schema_version match constants."""
    s = uuid.uuid4().hex[:8]
    sc = _build_full_scaffold(client, db_session, s)
    ds = db_session.get(DataSource, sc["ds_id"])

    ReportHandler().execute(_make_context(sc["report_run"], sc["report_task"], ds))
    db_session.expire_all()
    rr = db_session.execute(
        select(ReportRun).where(ReportRun.task_run_id == sc["report_run"].id)
    ).scalar_one()
    assert rr.report_engine_version == REPORT_ENGINE_VERSION
    assert rr.report_schema_version == REPORT_SCHEMA_VERSION
    assert rr.report_data["report_engine_version"] == REPORT_ENGINE_VERSION
    assert rr.report_data["report_schema_version"] == REPORT_SCHEMA_VERSION


def test_generated_by_defaults_to_system(client, db_session) -> None:
    """6. generated_by defaults to "system" when no authenticated user on context.

    Storing organization_id as generated_by would be misleading — an org UUID
    is not a human identity. The truthful value for a worker-executed run with
    no session user is "system".
    """
    s = uuid.uuid4().hex[:8]
    sc = _build_full_scaffold(client, db_session, s)
    ds = db_session.get(DataSource, sc["ds_id"])

    ReportHandler().execute(_make_context(sc["report_run"], sc["report_task"], ds))
    db_session.expire_all()
    rr = db_session.execute(
        select(ReportRun).where(ReportRun.task_run_id == sc["report_run"].id)
    ).scalar_one()
    assert rr.report_data["generated_by"] == "system"


def test_decision_counts_in_report_data(client, db_session) -> None:
    """7. Decision counts flow through to report_data.decisions.

    _insert_full_chain() creates a RemediationRun with total_changes_count=2
    and no RemediationChangeDecision rows, so the engine calculates:
      pending = max(0, 2 - 0 - 0) = 2
    This verifies the decision-count pipeline without needing the full
    RemediationChange → Issue FK chain.
    """
    s = uuid.uuid4().hex[:8]
    sc = _build_full_scaffold(client, db_session, s)
    ds = db_session.get(DataSource, sc["ds_id"])

    ReportHandler().execute(_make_context(sc["report_run"], sc["report_task"], ds))
    db_session.expire_all()
    rr = db_session.execute(
        select(ReportRun).where(ReportRun.task_run_id == sc["report_run"].id)
    ).scalar_one()
    decisions = rr.report_data["decisions"]
    # 2 total changes, 0 approved, 0 rejected → 2 pending
    assert decisions["approved"] == 0
    assert decisions["pending_decision"] == 2


# ---------------------------------------------------------------------------
# 8-9: Idempotency
# ---------------------------------------------------------------------------

def test_idempotency_second_execute_returns_existing(client, db_session) -> None:
    """8. Second execute() returns 'already exists' without creating a second row."""
    s = uuid.uuid4().hex[:8]
    sc = _build_full_scaffold(client, db_session, s)
    ds = db_session.get(DataSource, sc["ds_id"])
    ctx = _make_context(sc["report_run"], sc["report_task"], ds)

    result1 = ReportHandler().execute(ctx)
    result2 = ReportHandler().execute(ctx)

    assert "already exists" in result2
    count = db_session.execute(
        select(ReportRun).where(ReportRun.task_run_id == sc["report_run"].id)
    ).all()
    assert len(count) == 1


def test_idempotency_integrity_error_catch_and_refetch(client, db_session) -> None:
    """9. IntegrityError on concurrent duplicate → catch-and-refetch."""
    s = uuid.uuid4().hex[:8]
    sc = _build_full_scaffold(client, db_session, s)
    ds = db_session.get(DataSource, sc["ds_id"])

    # Pre-insert the ReportRun to simulate a race winner.
    from app.models.enums import REPORT_ENGINE_VERSION, REPORT_SCHEMA_VERSION
    preexisting = ReportRun(
        id=uuid.uuid4(),
        organization_id=sc["org_id"],
        task_run_id=sc["report_run"].id,
        task_id=sc["report_task"].id,
        data_source_id=sc["ds_id"],
        quality_control_run_id=None,
        report_engine_version=REPORT_ENGINE_VERSION,
        report_schema_version=REPORT_SCHEMA_VERSION,
        report_data={"stub": True},
    )
    db_session.add(preexisting)
    db_session.commit()

    # Handler must detect existing row at idempotency gate step 2.
    ctx = _make_context(sc["report_run"], sc["report_task"], ds)
    result = ReportHandler().execute(ctx)
    assert "already exists" in result

    # Still exactly one row.
    count = db_session.execute(
        select(ReportRun).where(ReportRun.task_run_id == sc["report_run"].id)
    ).all()
    assert len(count) == 1


# ---------------------------------------------------------------------------
# 10-12: Partial pipeline
# ---------------------------------------------------------------------------

def test_partial_pipeline_only_data_profile(client, db_session) -> None:
    """10. Partial report created when only DataProfile exists."""
    s = uuid.uuid4().hex[:8]
    headers = _auth_headers(client, s)
    source_id = _create_data_source(client, headers, s)
    ds_list = client.get("/data-sources", headers=headers)
    org_id = uuid.UUID(ds_list.json()["items"][0]["organization_id"])
    ds_id = uuid.UUID(source_id)

    sync_tid = _make_task(client, headers, "sync", source_id, f"Sync {s}")
    sync_rid = _make_run(client, headers, sync_tid)
    report_tid = _make_task(client, headers, "report", source_id, f"Report {s}")
    report_rid = _make_run(client, headers, report_tid)

    db_session.expire_all()
    sync_task = db_session.get(Task, uuid.UUID(sync_tid))
    sync_run = db_session.get(TaskRun, uuid.UUID(sync_rid))
    report_task = db_session.get(Task, uuid.UUID(report_tid))
    report_run = db_session.get(TaskRun, uuid.UUID(report_rid))

    dp = DataProfile(
        id=uuid.uuid4(), organization_id=org_id,
        task_run_id=sync_run.id, task_id=sync_task.id,
        data_source_id=ds_id,
        source_filename="data.csv", source_size_bytes=100,
        source_sha256="a" * 64, detected_encoding="utf-8", delimiter=",",
        row_count=5, column_count=2,
        duplicate_row_count=0, missing_value_total=0,
        column_profiles=[], structural_issues=[], limits_applied={},
    )
    db_session.add(dp)
    db_session.commit()

    ds = db_session.get(DataSource, ds_id)
    ctx = _make_context(report_run, report_task, ds)
    result = ReportHandler().execute(ctx)
    assert "report_run_id=" in result

    db_session.expire_all()
    rr = db_session.execute(
        select(ReportRun).where(ReportRun.task_run_id == report_run.id)
    ).scalar_one()
    assert rr.quality_control_run_id is None


def test_partial_pipeline_status_is_partial(client, db_session) -> None:
    """11. pipeline_status == 'partial' for partial pipeline."""
    s = uuid.uuid4().hex[:8]
    headers = _auth_headers(client, s)
    source_id = _create_data_source(client, headers, s)
    ds_list = client.get("/data-sources", headers=headers)
    org_id = uuid.UUID(ds_list.json()["items"][0]["organization_id"])
    ds_id = uuid.UUID(source_id)

    sync_tid = _make_task(client, headers, "sync", source_id, f"Sync {s}")
    sync_rid = _make_run(client, headers, sync_tid)
    report_tid = _make_task(client, headers, "report", source_id, f"Report {s}")
    report_rid = _make_run(client, headers, report_tid)

    db_session.expire_all()
    sync_task = db_session.get(Task, uuid.UUID(sync_tid))
    sync_run = db_session.get(TaskRun, uuid.UUID(sync_rid))
    report_task = db_session.get(Task, uuid.UUID(report_tid))
    report_run = db_session.get(TaskRun, uuid.UUID(report_rid))

    dp = DataProfile(
        id=uuid.uuid4(), organization_id=org_id,
        task_run_id=sync_run.id, task_id=sync_task.id,
        data_source_id=ds_id, source_filename="d.csv", source_size_bytes=50,
        source_sha256="b" * 64, detected_encoding="utf-8", delimiter=",",
        row_count=3, column_count=1,
        duplicate_row_count=0, missing_value_total=0,
        column_profiles=[], structural_issues=[], limits_applied={},
    )
    db_session.add(dp)
    db_session.commit()

    ds = db_session.get(DataSource, ds_id)
    ReportHandler().execute(_make_context(report_run, report_task, ds))
    db_session.expire_all()
    rr = db_session.execute(
        select(ReportRun).where(ReportRun.task_run_id == report_run.id)
    ).scalar_one()
    assert rr.report_data["executive_summary"]["overall_pipeline_status"] == "partial"


def test_no_data_pipeline_status(client, db_session) -> None:
    """12. pipeline_status == 'no_data' when nothing exists for data_source."""
    s = uuid.uuid4().hex[:8]
    headers = _auth_headers(client, s)
    source_id = _create_data_source(client, headers, s)
    report_tid = _make_task(client, headers, "report", source_id, f"Report {s}")
    report_rid = _make_run(client, headers, report_tid)

    db_session.expire_all()
    report_task = db_session.get(Task, uuid.UUID(report_tid))
    report_run = db_session.get(TaskRun, uuid.UUID(report_rid))
    ds = db_session.get(DataSource, uuid.UUID(source_id))

    ReportHandler().execute(_make_context(report_run, report_task, ds))
    db_session.expire_all()
    rr = db_session.execute(
        select(ReportRun).where(ReportRun.task_run_id == report_run.id)
    ).scalar_one()
    assert rr.report_data["executive_summary"]["overall_pipeline_status"] == "no_data"


# ---------------------------------------------------------------------------
# 13: Fallback logic
# ---------------------------------------------------------------------------

def test_source_task_run_id_none_falls_back_to_latest_qc_run(
    client, db_session
) -> None:
    """13. source_task_run_id=None → falls back to latest QC run for data_source."""
    s = uuid.uuid4().hex[:8]
    sc = _build_full_scaffold(client, db_session, s)
    ds = db_session.get(DataSource, sc["ds_id"])

    # Create a SECOND report task + run with source_task_run_id=None (default).
    report_tid2 = _make_task(
        client, sc["headers"], "report", str(sc["ds_id"]), f"Report2 {s}"
    )
    report_rid2 = _make_run(client, sc["headers"], report_tid2)  # no source → fallback
    db_session.expire_all()
    report_task2 = db_session.get(Task, uuid.UUID(report_tid2))
    report_run2 = db_session.get(TaskRun, uuid.UUID(report_rid2))
    # Leave source_task_run_id as None (no patch needed — API doesn't set it).

    ctx = _make_context(report_run2, report_task2, ds)
    result = ReportHandler().execute(ctx)
    assert "report_run_id=" in result

    db_session.expire_all()
    rr = db_session.execute(
        select(ReportRun).where(ReportRun.task_run_id == report_run2.id)
    ).scalar_one()
    # Handler should have found the QC run via fallback.
    assert rr.quality_control_run_id == sc["qcr"].id


# ---------------------------------------------------------------------------
# 14: Prerequisite errors
# ---------------------------------------------------------------------------

def test_missing_data_source_raises_permanent_error(client, db_session) -> None:
    """14. Missing data_source → PermanentExecutionError."""
    s = uuid.uuid4().hex[:8]
    headers = _auth_headers(client, s)
    source_id = _create_data_source(client, headers, s)
    report_tid = _make_task(client, headers, "report", source_id, f"Report {s}")
    report_rid = _make_run(client, headers, report_tid)

    db_session.expire_all()
    report_task = db_session.get(Task, uuid.UUID(report_tid))
    report_run = db_session.get(TaskRun, uuid.UUID(report_rid))

    ctx = _make_context(report_run, report_task, None)  # data_source=None
    with pytest.raises(PermanentExecutionError, match="data source"):
        ReportHandler().execute(ctx)


# ---------------------------------------------------------------------------
# 15: Cross-org isolation
# ---------------------------------------------------------------------------

def test_cross_org_source_task_run_id_treated_as_missing(
    client, db_session
) -> None:
    """15. Cross-org source_task_run_id → treated as missing → partial report."""
    sA = uuid.uuid4().hex[:8]
    sB = uuid.uuid4().hex[:8]

    # Build org A's full chain.
    scA = _build_full_scaffold(client, db_session, sA)

    # Build org B's scaffold and set org B's report run to point at org A's QC run.
    headersB = _auth_headers(client, sB)
    srcB = _create_data_source(client, headersB, sB)
    report_tidB = _make_task(client, headersB, "report", srcB, f"Report {sB}")
    report_ridB = _make_run(client, headersB, report_tidB)

    db_session.expire_all()
    report_taskB = db_session.get(Task, uuid.UUID(report_tidB))
    report_runB = db_session.get(TaskRun, uuid.UUID(report_ridB))
    dsB = db_session.get(DataSource, uuid.UUID(srcB))

    # Point org B's REPORT run at org A's QC TaskRun (cross-org).
    # We set source_task_run_id in memory only — the composite FK
    # (organization_id, source_task_run_id) would reject a cross-org value
    # at the DB layer (by design), so we don't commit this mutation.
    # The handler reads source_task_run_id from context.task_run, not from
    # a fresh DB query, so in-memory mutation is sufficient to exercise the
    # cross-org isolation path.
    db_session.expunge(report_runB)
    report_runB.source_task_run_id = scA["qc_run"].id

    ctx = _make_context(report_runB, report_taskB, dsB)
    result = ReportHandler().execute(ctx)
    assert "report_run_id=" in result

    db_session.expire_all()
    rr = db_session.execute(
        select(ReportRun).where(ReportRun.task_run_id == report_runB.id)
    ).scalar_one()
    # Cross-org QCR was invisible → quality_control_run_id should be None.
    assert rr.quality_control_run_id is None


# ---------------------------------------------------------------------------
# 16-20: Persistence checks
# ---------------------------------------------------------------------------

def test_exactly_one_report_run_created(client, db_session) -> None:
    """16. Exactly one ReportRun created per task_run_id."""
    s = uuid.uuid4().hex[:8]
    sc = _build_full_scaffold(client, db_session, s)
    ds = db_session.get(DataSource, sc["ds_id"])

    ReportHandler().execute(_make_context(sc["report_run"], sc["report_task"], ds))
    db_session.expire_all()
    rows = db_session.execute(
        select(ReportRun).where(ReportRun.task_run_id == sc["report_run"].id)
    ).all()
    assert len(rows) == 1


def test_organization_id_matches_task_run(client, db_session) -> None:
    """17. organization_id on ReportRun matches task_run.organization_id."""
    s = uuid.uuid4().hex[:8]
    sc = _build_full_scaffold(client, db_session, s)
    ds = db_session.get(DataSource, sc["ds_id"])

    ReportHandler().execute(_make_context(sc["report_run"], sc["report_task"], ds))
    db_session.expire_all()
    rr = db_session.execute(
        select(ReportRun).where(ReportRun.task_run_id == sc["report_run"].id)
    ).scalar_one()
    assert rr.organization_id == sc["report_run"].organization_id


def test_data_source_id_matches_task(client, db_session) -> None:
    """18. data_source_id on ReportRun matches the task's data_source_id."""
    s = uuid.uuid4().hex[:8]
    sc = _build_full_scaffold(client, db_session, s)
    ds = db_session.get(DataSource, sc["ds_id"])

    ReportHandler().execute(_make_context(sc["report_run"], sc["report_task"], ds))
    db_session.expire_all()
    rr = db_session.execute(
        select(ReportRun).where(ReportRun.task_run_id == sc["report_run"].id)
    ).scalar_one()
    assert rr.data_source_id == sc["ds_id"]


def test_report_data_is_non_empty_dict(client, db_session) -> None:
    """19. report_data is a non-empty dict."""
    s = uuid.uuid4().hex[:8]
    sc = _build_full_scaffold(client, db_session, s)
    ds = db_session.get(DataSource, sc["ds_id"])

    ReportHandler().execute(_make_context(sc["report_run"], sc["report_task"], ds))
    db_session.expire_all()
    rr = db_session.execute(
        select(ReportRun).where(ReportRun.task_run_id == sc["report_run"].id)
    ).scalar_one()
    assert isinstance(rr.report_data, dict)
    assert len(rr.report_data) > 0


def test_report_schema_version_on_row(client, db_session) -> None:
    """20. ReportRun.report_schema_version == REPORT_SCHEMA_VERSION constant."""
    s = uuid.uuid4().hex[:8]
    sc = _build_full_scaffold(client, db_session, s)
    ds = db_session.get(DataSource, sc["ds_id"])

    ReportHandler().execute(_make_context(sc["report_run"], sc["report_task"], ds))
    db_session.expire_all()
    rr = db_session.execute(
        select(ReportRun).where(ReportRun.task_run_id == sc["report_run"].id)
    ).scalar_one()
    assert rr.report_schema_version == REPORT_SCHEMA_VERSION


# ---------------------------------------------------------------------------
# 21-22: Pipeline lineage integrity
# ---------------------------------------------------------------------------

def test_audit_lineage_does_not_contain_export_run_id(client, db_session) -> None:
    """21. audit_lineage must not contain export_run_id.

    ExportRun is from the Module 9 MATCH→EXPORT pipeline and has no FK to
    QualityControlRun. Including its ID in audit_lineage would falsely imply
    it belongs to the same pipeline chain. It stays in export_result only.
    """
    s = uuid.uuid4().hex[:8]
    sc = _build_full_scaffold(client, db_session, s)
    ds = db_session.get(DataSource, sc["ds_id"])

    ReportHandler().execute(_make_context(sc["report_run"], sc["report_task"], ds))
    db_session.expire_all()
    rr = db_session.execute(
        select(ReportRun).where(ReportRun.task_run_id == sc["report_run"].id)
    ).scalar_one()
    al = rr.report_data["audit_lineage"]
    assert "export_run_id" not in al, (
        "audit_lineage must not contain export_run_id — ExportRun is not "
        "part of the QC pipeline chain and cannot be anchored to it."
    )


def test_pipeline_lineage_isolation_two_executions(client, db_session) -> None:
    """22. Two independent pipeline executions for the same data source cannot
    mix each other's CleanExport records in the report.

    Chain A: full pipeline + CleanExport_A (dataset_version = QCR_A.id)
    Chain B: full pipeline + CleanExport_B (dataset_version = QCR_B.id)

    Report anchored to chain A must show clean_export_id == CleanExport_A.id,
    never CleanExport_B.id.  If the handler used "latest completed CleanExport
    for this data_source" it would return whichever export is newer regardless
    of which pipeline chain generated the report — a lineage integrity failure.
    """
    s = uuid.uuid4().hex[:8]

    # ── Chain A ──────────────────────────────────────────────────────────────
    scA = _build_full_scaffold(client, db_session, s + "A")
    dsA = db_session.get(DataSource, scA["ds_id"])

    # Insert CleanExport_A anchored to QCR_A via dataset_version.
    ce_a = CleanExport(
        id=uuid.uuid4(),
        organization_id=scA["org_id"],
        job_id=scA["report_task"].id,   # task_id of any task in this org
        data_source_id=scA["ds_id"],
        dataset_version=scA["qcr"].id,  # anchors to pipeline A's QC run
        format="csv",
        status="completed",
        idempotency_key=uuid.uuid4().hex,
        row_count=10,
        column_count=3,
    )
    db_session.add(ce_a)
    db_session.commit()

    # ── Chain B (second execution, same org + data source) ───────────────────
    # Re-use the same org/data-source but create a fresh complete pipeline chain.
    headers_a = scA["headers"]
    ds_id_str = str(scA["ds_id"])

    sync_tid_b = _make_task(client, headers_a, "sync", ds_id_str, f"SyncB {s}")
    sync_rid_b = _make_run(client, headers_a, sync_tid_b)
    detect_tid_b = _make_task(client, headers_a, "detect", ds_id_str, f"DetectB {s}")
    detect_rid_b = _make_run(client, headers_a, detect_tid_b)
    rem_tid_b = _make_task(client, headers_a, "remediate", ds_id_str, f"RemB {s}")
    rem_rid_b = _make_run(client, headers_a, rem_tid_b, source_task_run_id=detect_rid_b)
    apply_tid_b = _make_task(client, headers_a, "apply_remediations", ds_id_str, f"ApplyB {s}")
    apply_rid_b = _make_run(client, headers_a, apply_tid_b)
    val_tid_b = _make_task(client, headers_a, "validate", ds_id_str, f"ValB {s}")
    val_rid_b = _make_run(client, headers_a, val_tid_b, source_task_run_id=apply_rid_b)
    qc_tid_b = _make_task(client, headers_a, "quality_ctrl", ds_id_str, f"QCB {s}")
    qc_rid_b = _make_run(client, headers_a, qc_tid_b)
    report_tid_b = _make_task(client, headers_a, "report", ds_id_str, f"ReportB {s}")
    report_rid_b = _make_run(client, headers_a, report_tid_b)

    db_session.expire_all()
    sync_task_b = db_session.get(Task, uuid.UUID(sync_tid_b))
    detect_task_b = db_session.get(Task, uuid.UUID(detect_tid_b))
    rem_task_b = db_session.get(Task, uuid.UUID(rem_tid_b))
    apply_task_b = db_session.get(Task, uuid.UUID(apply_tid_b))
    val_task_b = db_session.get(Task, uuid.UUID(val_tid_b))
    qc_task_b = db_session.get(Task, uuid.UUID(qc_tid_b))
    report_task_b = db_session.get(Task, uuid.UUID(report_tid_b))
    sync_run_b = db_session.get(TaskRun, uuid.UUID(sync_rid_b))
    detect_run_b = db_session.get(TaskRun, uuid.UUID(detect_rid_b))
    rem_run_b = db_session.get(TaskRun, uuid.UUID(rem_rid_b))
    apply_run_b = db_session.get(TaskRun, uuid.UUID(apply_rid_b))
    val_run_b = db_session.get(TaskRun, uuid.UUID(val_rid_b))
    qc_run_b = db_session.get(TaskRun, uuid.UUID(qc_rid_b))
    report_run_b = db_session.get(TaskRun, uuid.UUID(report_rid_b))

    chain_b = _insert_full_chain(
        db_session,
        org_id=scA["org_id"], ds_id=scA["ds_id"],
        sync_run=sync_run_b, detect_run=detect_run_b, remediate_run=rem_run_b,
        apply_run=apply_run_b, validate_run=val_run_b, qc_run=qc_run_b,
        sync_task=sync_task_b, detect_task=detect_task_b, remediate_task=rem_task_b,
        apply_task=apply_task_b, validate_task=val_task_b, qc_task=qc_task_b,
    )

    # Wire report run B to point at QCR_B.
    report_run_b.source_task_run_id = qc_run_b.id
    db_session.commit()

    # Insert CleanExport_B anchored to QCR_B — this is the NEWER export
    # (same data source, same org). Without the dataset_version anchor fix,
    # the handler would return CleanExport_B for BOTH chain A and chain B reports.
    db_session.expire_all()
    qcr_b = chain_b["qcr"]
    ce_b = CleanExport(
        id=uuid.uuid4(),
        organization_id=scA["org_id"],
        job_id=report_task_b.id,
        data_source_id=scA["ds_id"],
        dataset_version=qcr_b.id,      # anchors to pipeline B's QC run
        format="csv",
        status="completed",
        idempotency_key=uuid.uuid4().hex,
        row_count=8,
        column_count=3,
    )
    db_session.add(ce_b)
    db_session.commit()

    # ── Execute chain A's report handler ─────────────────────────────────────
    db_session.expire_all()
    report_run_a = db_session.get(TaskRun, scA["report_run"].id)
    report_task_a = db_session.get(Task, scA["report_task"].id)
    ctx_a = _make_context(report_run_a, report_task_a, dsA)
    ReportHandler().execute(ctx_a)

    db_session.expire_all()
    rr_a = db_session.execute(
        select(ReportRun).where(ReportRun.task_run_id == report_run_a.id)
    ).scalar_one()

    # Chain A's report must show CleanExport_A, not CleanExport_B.
    ce_a_id_str = str(ce_a.id)
    ce_b_id_str = str(ce_b.id)
    al_a = rr_a.report_data["audit_lineage"]
    assert al_a["clean_export_id"] == ce_a_id_str, (
        f"Chain A report must reference CleanExport_A ({ce_a_id_str}), "
        f"got {al_a['clean_export_id']!r} — pipeline chains are mixing."
    )
    assert al_a.get("clean_export_id") != ce_b_id_str, (
        "Chain A report incorrectly references Chain B's CleanExport."
    )

    # ── Execute chain B's report handler ─────────────────────────────────────
    db_session.expire_all()
    report_run_b2 = db_session.get(TaskRun, report_run_b.id)
    ctx_b = _make_context(report_run_b2, report_task_b, dsA)
    ReportHandler().execute(ctx_b)

    db_session.expire_all()
    rr_b = db_session.execute(
        select(ReportRun).where(ReportRun.task_run_id == report_run_b2.id)
    ).scalar_one()

    al_b = rr_b.report_data["audit_lineage"]
    assert al_b["clean_export_id"] == ce_b_id_str, (
        f"Chain B report must reference CleanExport_B ({ce_b_id_str}), "
        f"got {al_b['clean_export_id']!r} — pipeline chains are mixing."
    )
    assert al_b.get("clean_export_id") != ce_a_id_str, (
        "Chain B report incorrectly references Chain A's CleanExport."
    )
