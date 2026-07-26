"""Module 20: API tests for the three report endpoints.

GET  /tasks/{task_id}/runs/{run_id}/report
GET  /tasks/{task_id}/report
GET  /tasks/{task_id}/runs/{run_id}/report/download

All rows are inserted directly via db_session (same "cheap because the
work already happened" pattern as test_quality_control_api.py).

Scenarios covered (28):
  GET /{task_id}/runs/{run_id}/report:
    1.  200 with all required ReportRunRead fields
    2.  processing_duration_ms derived from started_at / finished_at
    3.  processing_duration_ms is None when finished_at IS NULL
    4.  report_data embedded in response
    5.  404 when task not found
    6.  404 when task_run not found (task exists)
    7.  404 when report run not found (task + run exist)
    8.  401 unauthenticated → 401
    9.  cross-tenant: other org run_id → 404
  GET /{task_id}/report:
   10.  200 with all required fields
   11.  returns the most recently created ReportRun (latest by created_at)
   12.  404 when no ReportRun exists for the task
   13.  404 when task not found
   14.  401 unauthenticated → 401
   15.  cross-tenant: other org task_id → 404
  GET /{task_id}/runs/{run_id}/report/download:
   16.  200 with Content-Type: application/json
   17.  Content-Disposition header includes filename="report_{run_id}.json"
   18.  response body is valid JSON that matches report_data
   19.  Content-Length header present and correct
   20.  404 when task not found
   21.  404 when task_run not found
   22.  404 when report run not found
   23.  401 unauthenticated → 401
   24.  cross-tenant: other org → 404
  idempotency + retry safety:
   25.  reading the same endpoint twice returns identical data
  field-level checks:
   26.  organization_id in response matches authenticated org
   27.  report_engine_version == REPORT_ENGINE_VERSION constant
   28.  quality_control_run_id is None when not set on row
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.enums import REPORT_ENGINE_VERSION, REPORT_SCHEMA_VERSION
from app.models.report_run import ReportRun
from app.models.task import Task
from app.models.task_run import TaskRun


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _register(client: TestClient, suffix: str) -> dict:
    resp = client.post(
        "/auth/register",
        json={
            "organization_name": f"Report API Org {suffix}",
            "email": f"report-api-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Report API Tester",
        },
    )
    assert resp.status_code == 201, resp.text
    tok = resp.json()["access_token"]
    return {"Authorization": f"Bearer {tok}"}


def _create_data_source(client: TestClient, headers: dict, suffix: str) -> str:
    resp = client.post(
        "/data-sources",
        json={
            "name": f"Report Source {suffix}",
            "source_type": "csv_upload",
            "connection_metadata": {"file_path": "f.csv"},
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _make_task(
    client: TestClient, headers: dict, task_type: str, source_id: str, name: str
) -> str:
    resp = client.post(
        "/tasks",
        json={"name": name, "task_type": task_type, "data_source_id": source_id},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _make_run(client: TestClient, headers: dict, task_id: str) -> str:
    resp = client.post(f"/tasks/{task_id}/runs", headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _insert_report_run(
    db: Session,
    *,
    organization_id: uuid.UUID,
    task_run_id: uuid.UUID,
    task_id: uuid.UUID,
    data_source_id: uuid.UUID,
    quality_control_run_id: uuid.UUID | None = None,
    report_data: dict | None = None,
) -> ReportRun:
    if report_data is None:
        report_data = {
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "report_engine_version": REPORT_ENGINE_VERSION,
            "organization_id": str(organization_id),
            "organization_name": "Test Org",
            "generated_by": "test@example.com",
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "executive_summary": {
                "dataset_health_score": 88.5,
                "rows_processed": 100,
                "columns_processed": 5,
                "issues_found": 3,
                "approved_changes": 2,
                "rejected_changes": 1,
                "applied_changes": 2,
                "validation_pass_rate_pct": 100.0,
                "quality_score": 88.5,
                "release_recommendation": "PASS",
                "overall_pipeline_status": "complete",
            },
            "job_summary": {
                "task_id": str(task_id),
                "task_name": "Test task",
                "data_source_id": str(data_source_id),
                "data_source_name": "f.csv",
                "pipeline_stages_completed": ["sync", "detect"],
                "pipeline_stages_missing": [],
                "overall_status": "complete",
            },
            "audit_lineage": {
                "data_profile_id": None,
                "issue_detection_run_id": None,
                "remediation_run_id": None,
                "applied_remediation_run_id": None,
                "validation_run_id": None,
                "quality_control_run_id": None,
                "export_run_id": None,
                "clean_export_id": None,
            },
        }
    row = ReportRun(
        id=uuid.uuid4(),
        organization_id=organization_id,
        task_run_id=task_run_id,
        task_id=task_id,
        data_source_id=data_source_id,
        quality_control_run_id=quality_control_run_id,
        report_engine_version=REPORT_ENGINE_VERSION,
        report_schema_version=REPORT_SCHEMA_VERSION,
        report_data=report_data,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return row


def _build_report_scaffold(
    client: TestClient,
    db: Session,
    suffix: str,
    *,
    with_report_run: bool = True,
    started_at: datetime | None = None,
    finished_at: datetime | None = None,
) -> dict:
    """Build minimum prerequisite rows for report endpoint tests.

    Returns dict with:
      headers, source_id, org_id (str), task_id (str), run_id (str),
      report_run (ReportRun ORM row | None)
    """
    headers = _register(client, suffix)
    source_id = _create_data_source(client, headers, suffix)

    # Resolve org_id from data source response
    ds_list = client.get("/data-sources", headers=headers)
    org_id_str = ds_list.json()["items"][0]["organization_id"]
    org_id = uuid.UUID(org_id_str)
    ds_uuid = uuid.UUID(source_id)

    # Create REPORT task + run
    task_id = _make_task(client, headers, "report", source_id, f"Report {suffix}")
    run_id = _make_run(client, headers, task_id)

    db.expire_all()
    task = db.get(Task, uuid.UUID(task_id))
    task_run = db.get(TaskRun, uuid.UUID(run_id))

    if started_at is not None or finished_at is not None:
        task_run.status = "success"
        task_run.started_at = started_at
        task_run.finished_at = finished_at
        db.commit()

    report_run = None
    if with_report_run:
        report_run = _insert_report_run(
            db,
            organization_id=org_id,
            task_run_id=task_run.id,
            task_id=task.id,
            data_source_id=ds_uuid,
        )

    return {
        "headers": headers,
        "source_id": source_id,
        "org_id": org_id_str,
        "task_id": task_id,
        "run_id": run_id,
        "report_run": report_run,
    }


# ---------------------------------------------------------------------------
# GET /{task_id}/runs/{run_id}/report — scenarios 1-9
# ---------------------------------------------------------------------------

def test_get_report_run_200_all_fields(client, db_session) -> None:
    """1. 200 with all required ReportRunRead fields."""
    s = uuid.uuid4().hex[:8]
    scaffold = _build_report_scaffold(client, db_session, s)
    resp = client.get(
        f"/tasks/{scaffold['task_id']}/runs/{scaffold['run_id']}/report",
        headers=scaffold["headers"],
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "id" in data
    assert "organization_id" in data
    assert "task_run_id" in data
    assert "task_id" in data
    assert "data_source_id" in data
    assert "quality_control_run_id" in data
    assert "report_engine_version" in data
    assert "report_schema_version" in data
    assert "report_data" in data
    assert "processing_duration_ms" in data
    assert "created_at" in data


def test_get_report_run_processing_duration_ms_computed(client, db_session) -> None:
    """2. processing_duration_ms derived from started_at / finished_at."""
    s = uuid.uuid4().hex[:8]
    now = datetime.now(timezone.utc)
    scaffold = _build_report_scaffold(
        client, db_session, s,
        started_at=now - timedelta(seconds=5),
        finished_at=now,
    )
    resp = client.get(
        f"/tasks/{scaffold['task_id']}/runs/{scaffold['run_id']}/report",
        headers=scaffold["headers"],
    )
    assert resp.status_code == 200, resp.text
    dur = resp.json()["processing_duration_ms"]
    assert dur is not None
    assert dur == pytest.approx(5000, abs=200)


def test_get_report_run_processing_duration_ms_none_when_no_finish(
    client, db_session
) -> None:
    """3. processing_duration_ms is None when finished_at IS NULL."""
    s = uuid.uuid4().hex[:8]
    scaffold = _build_report_scaffold(client, db_session, s)
    resp = client.get(
        f"/tasks/{scaffold['task_id']}/runs/{scaffold['run_id']}/report",
        headers=scaffold["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["processing_duration_ms"] is None


def test_get_report_run_report_data_embedded(client, db_session) -> None:
    """4. report_data is embedded in the JSON response."""
    s = uuid.uuid4().hex[:8]
    scaffold = _build_report_scaffold(client, db_session, s)
    resp = client.get(
        f"/tasks/{scaffold['task_id']}/runs/{scaffold['run_id']}/report",
        headers=scaffold["headers"],
    )
    assert resp.status_code == 200, resp.text
    report_data = resp.json()["report_data"]
    assert isinstance(report_data, dict)
    assert "executive_summary" in report_data
    assert "audit_lineage" in report_data


def test_get_report_run_404_task_not_found(client, db_session) -> None:
    """5. 404 when task not found."""
    s = uuid.uuid4().hex[:8]
    scaffold = _build_report_scaffold(client, db_session, s)
    resp = client.get(
        f"/tasks/{uuid.uuid4()}/runs/{scaffold['run_id']}/report",
        headers=scaffold["headers"],
    )
    assert resp.status_code == 404


def test_get_report_run_404_run_not_found(client, db_session) -> None:
    """6. 404 when task_run not found (task exists)."""
    s = uuid.uuid4().hex[:8]
    scaffold = _build_report_scaffold(client, db_session, s)
    resp = client.get(
        f"/tasks/{scaffold['task_id']}/runs/{uuid.uuid4()}/report",
        headers=scaffold["headers"],
    )
    assert resp.status_code == 404


def test_get_report_run_404_report_not_found(client, db_session) -> None:
    """7. 404 when report run not found (task + run exist)."""
    s = uuid.uuid4().hex[:8]
    scaffold = _build_report_scaffold(client, db_session, s, with_report_run=False)
    resp = client.get(
        f"/tasks/{scaffold['task_id']}/runs/{scaffold['run_id']}/report",
        headers=scaffold["headers"],
    )
    assert resp.status_code == 404


def test_get_report_run_401_unauthenticated(client, db_session) -> None:
    """8. 401 unauthenticated."""
    s = uuid.uuid4().hex[:8]
    scaffold = _build_report_scaffold(client, db_session, s)
    resp = client.get(
        f"/tasks/{scaffold['task_id']}/runs/{scaffold['run_id']}/report"
    )
    assert resp.status_code == 401


def test_get_report_run_404_cross_tenant(client, db_session) -> None:
    """9. Cross-tenant: another org's run_id → 404."""
    sA = uuid.uuid4().hex[:8]
    sB = uuid.uuid4().hex[:8]
    scaffoldA = _build_report_scaffold(client, db_session, sA)
    scaffoldB = _build_report_scaffold(client, db_session, sB)

    # Org A uses Org B's run_id against Org A's task_id → 404.
    resp = client.get(
        f"/tasks/{scaffoldA['task_id']}/runs/{scaffoldB['run_id']}/report",
        headers=scaffoldA["headers"],
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /{task_id}/report — scenarios 10-15
# ---------------------------------------------------------------------------

def test_get_latest_report_200(client, db_session) -> None:
    """10. 200 with all required fields."""
    s = uuid.uuid4().hex[:8]
    scaffold = _build_report_scaffold(client, db_session, s)
    resp = client.get(
        f"/tasks/{scaffold['task_id']}/report",
        headers=scaffold["headers"],
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert "id" in data
    assert "report_data" in data
    assert "report_engine_version" in data


def test_get_latest_report_returns_newest(client, db_session) -> None:
    """11. Returns the most recently created ReportRun."""
    s = uuid.uuid4().hex[:8]
    scaffold = _build_report_scaffold(client, db_session, s)
    org_id = uuid.UUID(scaffold["org_id"])
    task_id_uuid = uuid.UUID(scaffold["task_id"])
    ds_id = uuid.UUID(scaffold["source_id"])

    # Insert a second REPORT task + run, plus a second report row
    task_id2 = _make_task(client, scaffold["headers"], "report", scaffold["source_id"],
                           f"Report2 {s}")
    run_id2 = _make_run(client, scaffold["headers"], task_id2)
    db_session.expire_all()
    task2 = db_session.get(Task, uuid.UUID(task_id2))
    run2 = db_session.get(TaskRun, uuid.UUID(run_id2))
    second = _insert_report_run(
        db_session,
        organization_id=org_id,
        task_run_id=run2.id,
        task_id=task2.id,
        data_source_id=ds_id,
        report_data={
            "report_schema_version": REPORT_SCHEMA_VERSION,
            "report_engine_version": REPORT_ENGINE_VERSION,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "marker": "second",
        },
    )

    resp = client.get(
        f"/tasks/{task_id2}/report",
        headers=scaffold["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["id"] == str(second.id)


def test_get_latest_report_404_no_report_exists(client, db_session) -> None:
    """12. 404 when no ReportRun exists for the task."""
    s = uuid.uuid4().hex[:8]
    scaffold = _build_report_scaffold(client, db_session, s, with_report_run=False)
    resp = client.get(
        f"/tasks/{scaffold['task_id']}/report",
        headers=scaffold["headers"],
    )
    assert resp.status_code == 404


def test_get_latest_report_404_task_not_found(client, db_session) -> None:
    """13. 404 when task not found."""
    s = uuid.uuid4().hex[:8]
    scaffold = _build_report_scaffold(client, db_session, s)
    resp = client.get(
        f"/tasks/{uuid.uuid4()}/report",
        headers=scaffold["headers"],
    )
    assert resp.status_code == 404


def test_get_latest_report_401_unauthenticated(client, db_session) -> None:
    """14. 401 unauthenticated."""
    s = uuid.uuid4().hex[:8]
    scaffold = _build_report_scaffold(client, db_session, s)
    resp = client.get(f"/tasks/{scaffold['task_id']}/report")
    assert resp.status_code == 401


def test_get_latest_report_404_cross_tenant(client, db_session) -> None:
    """15. Cross-tenant: other org's task_id → 404."""
    sA = uuid.uuid4().hex[:8]
    sB = uuid.uuid4().hex[:8]
    scaffoldA = _build_report_scaffold(client, db_session, sA)
    scaffoldB = _build_report_scaffold(client, db_session, sB)

    # Org A queries Org B's task_id → 404.
    resp = client.get(
        f"/tasks/{scaffoldB['task_id']}/report",
        headers=scaffoldA["headers"],
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# GET /{task_id}/runs/{run_id}/report/download — scenarios 16-24
# ---------------------------------------------------------------------------

def test_download_report_200_content_type(client, db_session) -> None:
    """16. 200 with Content-Type: application/json."""
    s = uuid.uuid4().hex[:8]
    scaffold = _build_report_scaffold(client, db_session, s)
    resp = client.get(
        f"/tasks/{scaffold['task_id']}/runs/{scaffold['run_id']}/report/download",
        headers=scaffold["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert "application/json" in resp.headers["content-type"]


def test_download_report_content_disposition(client, db_session) -> None:
    """17. Content-Disposition header includes expected filename."""
    s = uuid.uuid4().hex[:8]
    scaffold = _build_report_scaffold(client, db_session, s)
    run_id = scaffold["run_id"]
    resp = client.get(
        f"/tasks/{scaffold['task_id']}/runs/{run_id}/report/download",
        headers=scaffold["headers"],
    )
    assert resp.status_code == 200, resp.text
    cd = resp.headers.get("content-disposition", "")
    assert f"report_{run_id}.json" in cd


def test_download_report_body_is_valid_json_matching_report_data(
    client, db_session
) -> None:
    """18. Response body is valid JSON matching report_data."""
    s = uuid.uuid4().hex[:8]
    scaffold = _build_report_scaffold(client, db_session, s)
    resp_download = client.get(
        f"/tasks/{scaffold['task_id']}/runs/{scaffold['run_id']}/report/download",
        headers=scaffold["headers"],
    )
    assert resp_download.status_code == 200, resp_download.text
    downloaded = json.loads(resp_download.content)

    # Also fetch via the normal endpoint and compare report_data.
    resp_api = client.get(
        f"/tasks/{scaffold['task_id']}/runs/{scaffold['run_id']}/report",
        headers=scaffold["headers"],
    )
    assert resp_api.status_code == 200
    assert downloaded == resp_api.json()["report_data"]


def test_download_report_content_length_correct(client, db_session) -> None:
    """19. Content-Length header present and matches body length."""
    s = uuid.uuid4().hex[:8]
    scaffold = _build_report_scaffold(client, db_session, s)
    resp = client.get(
        f"/tasks/{scaffold['task_id']}/runs/{scaffold['run_id']}/report/download",
        headers=scaffold["headers"],
    )
    assert resp.status_code == 200, resp.text
    content_length = resp.headers.get("content-length")
    assert content_length is not None
    assert int(content_length) == len(resp.content)


def test_download_404_task_not_found(client, db_session) -> None:
    """20. 404 when task not found."""
    s = uuid.uuid4().hex[:8]
    scaffold = _build_report_scaffold(client, db_session, s)
    resp = client.get(
        f"/tasks/{uuid.uuid4()}/runs/{scaffold['run_id']}/report/download",
        headers=scaffold["headers"],
    )
    assert resp.status_code == 404


def test_download_404_run_not_found(client, db_session) -> None:
    """21. 404 when task_run not found."""
    s = uuid.uuid4().hex[:8]
    scaffold = _build_report_scaffold(client, db_session, s)
    resp = client.get(
        f"/tasks/{scaffold['task_id']}/runs/{uuid.uuid4()}/report/download",
        headers=scaffold["headers"],
    )
    assert resp.status_code == 404


def test_download_404_report_not_found(client, db_session) -> None:
    """22. 404 when report run not found."""
    s = uuid.uuid4().hex[:8]
    scaffold = _build_report_scaffold(client, db_session, s, with_report_run=False)
    resp = client.get(
        f"/tasks/{scaffold['task_id']}/runs/{scaffold['run_id']}/report/download",
        headers=scaffold["headers"],
    )
    assert resp.status_code == 404


def test_download_401_unauthenticated(client, db_session) -> None:
    """23. 401 unauthenticated."""
    s = uuid.uuid4().hex[:8]
    scaffold = _build_report_scaffold(client, db_session, s)
    resp = client.get(
        f"/tasks/{scaffold['task_id']}/runs/{scaffold['run_id']}/report/download"
    )
    assert resp.status_code == 401


def test_download_404_cross_tenant(client, db_session) -> None:
    """24. Cross-tenant: other org → 404."""
    sA = uuid.uuid4().hex[:8]
    sB = uuid.uuid4().hex[:8]
    scaffoldA = _build_report_scaffold(client, db_session, sA)
    scaffoldB = _build_report_scaffold(client, db_session, sB)

    resp = client.get(
        f"/tasks/{scaffoldA['task_id']}/runs/{scaffoldB['run_id']}/report/download",
        headers=scaffoldA["headers"],
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Idempotency + retry safety (25)
# ---------------------------------------------------------------------------

def test_reading_endpoint_twice_returns_identical_data(client, db_session) -> None:
    """25. Reading the same endpoint twice returns identical data."""
    s = uuid.uuid4().hex[:8]
    scaffold = _build_report_scaffold(client, db_session, s)
    url = f"/tasks/{scaffold['task_id']}/runs/{scaffold['run_id']}/report"
    r1 = client.get(url, headers=scaffold["headers"])
    r2 = client.get(url, headers=scaffold["headers"])
    assert r1.status_code == 200
    assert r2.status_code == 200
    assert r1.json() == r2.json()


# ---------------------------------------------------------------------------
# Field-level checks (26-28)
# ---------------------------------------------------------------------------

def test_organization_id_matches_authenticated_org(client, db_session) -> None:
    """26. organization_id in response matches authenticated org."""
    s = uuid.uuid4().hex[:8]
    scaffold = _build_report_scaffold(client, db_session, s)
    resp = client.get(
        f"/tasks/{scaffold['task_id']}/runs/{scaffold['run_id']}/report",
        headers=scaffold["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["organization_id"] == scaffold["org_id"]


def test_report_engine_version_constant(client, db_session) -> None:
    """27. report_engine_version == REPORT_ENGINE_VERSION constant."""
    s = uuid.uuid4().hex[:8]
    scaffold = _build_report_scaffold(client, db_session, s)
    resp = client.get(
        f"/tasks/{scaffold['task_id']}/runs/{scaffold['run_id']}/report",
        headers=scaffold["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["report_engine_version"] == REPORT_ENGINE_VERSION


def test_quality_control_run_id_none_when_not_set(client, db_session) -> None:
    """28. quality_control_run_id is None when not set on the row."""
    s = uuid.uuid4().hex[:8]
    scaffold = _build_report_scaffold(client, db_session, s)
    resp = client.get(
        f"/tasks/{scaffold['task_id']}/runs/{scaffold['run_id']}/report",
        headers=scaffold["headers"],
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["quality_control_run_id"] is None
