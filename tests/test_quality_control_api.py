"""Module 18 Phase 3: API tests for the two read-only quality control endpoints.

GET /tasks/{task_id}/runs/{run_id}/quality-control
GET /tasks/{task_id}/runs/{run_id}/quality-control/findings

Rows are inserted directly via db_session (never through the real worker
handler/CSV pipeline) since these endpoints only read already-persisted rows —
same "cheap because the work already happened" justification that
test_validation_api.py and test_remediation_api.py both document.

The prerequisite chain for a QualityControlRun is:
  Organization → DataSource
  → Task[SYNC] → TaskRun[SYNC] → DataProfile
  → Task[DETECT] → TaskRun[DETECT] → IssueDetectionRun
  → Task[REMEDIATE] → TaskRun[REMEDIATE] → RemediationRun → RemediationChange
  → Task[VALIDATE] → TaskRun[VALIDATE] → ValidationRun → ValidationResult
  → Task[QUALITY_CTRL] → TaskRun[QUALITY_CTRL]
  → QualityControlRun → QualityFinding (0..N)

Scenarios covered (22):
  summary endpoint:
    1. 200 with all required fields
    2. processing_duration_ms derived from started_at / finished_at
    3. processing_duration_ms is None when finished_at is NULL
    4. 404 when task not found
    5. 404 when task_run not found (task found)
    6. 404 when quality control run not found (task + run found)
    7. cross-tenant: other org's run → 404
    8. unauthenticated → 401
  findings endpoint:
    9. 200 with correct count and all fields
   10. filter by category (valid)
   11. filter by severity (valid)
   12. filter by outcome (valid)
   13. filter by rule_name
   14. filter by affected_column
   15. multiple filters combined (AND semantics)
   16. 422 for unknown category
   17. 422 for unknown severity
   18. 422 for unknown outcome
   19. stable sort order (created_at ASC, id ASC)
   20. pagination: limit + offset
   21. empty result when no findings exist
   22. cross-tenant: other org's run → 404
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.data_profile import DataProfile
from app.models.enums import QUALITY_CATEGORIES
from app.models.issue_detection_run import IssueDetectionRun
from app.models.quality_control_run import QualityControlRun
from app.models.quality_finding import QualityFinding
from app.models.remediation_run import RemediationRun
from app.models.task import Task
from app.models.task_run import TaskRun
from app.models.validation_run import ValidationRun
from app.quality.engine import QUALITY_ENGINE_VERSION


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register(client: TestClient, suffix: str) -> dict:
    resp = client.post(
        "/auth/register",
        json={
            "organization_name": f"QC API Org {suffix}",
            "email": f"qc-api-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "QC API Tester",
        },
    )
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _create_data_source(client: TestClient, headers: dict, suffix: str) -> str:
    resp = client.post(
        "/data-sources",
        json={
            "name": f"QC Source {suffix}",
            "source_type": "csv_upload",
            "connection_metadata": {"file_path": "f.csv"},
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _make_task(client: TestClient, headers: dict, task_type: str,
               source_id: str, name: str) -> str:
    resp = client.post(
        "/tasks",
        json={"name": name, "task_type": task_type, "data_source_id": source_id},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _make_run(client: TestClient, headers: dict, task_id: str,
              source_task_run_id: str | None = None) -> str:
    body = {}
    if source_task_run_id:
        body["source_task_run_id"] = source_task_run_id
    resp = client.post(f"/tasks/{task_id}/runs", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _build_qc_scaffold(client: TestClient, db: Session, suffix: str) -> dict:
    """Build the minimum prerequisite rows needed for the quality-control read
    endpoints. Inserts domain rows directly via ORM; task/run rows go via API.

    Full chain: SYNC → DETECT → REMEDIATE → VALIDATE → QUALITY_CTRL.
    All FK rows are real rows (SQLite enforces PRAGMA foreign_keys=ON).
    QUALITY_CTRL task/run created via API (no source_task_run_id — not in the
    allow-list), and source_task_run_id is patched in DB afterward.
    """
    headers = _register(client, suffix)

    ds_id = _create_data_source(client, headers, suffix)
    ds_resp = client.get("/data-sources", headers=headers)
    assert ds_resp.status_code == 200, ds_resp.text
    org_id = uuid.UUID(ds_resp.json()["items"][0]["organization_id"])
    ds_uuid = uuid.UUID(ds_id)

    # ── TaskRun chain (all via API) ──────────────────────────────────────
    sync_task_id = _make_task(client, headers, "sync", ds_id, f"Sync {suffix}")
    sync_run_id = _make_run(client, headers, sync_task_id)

    detect_task_id = _make_task(client, headers, "detect", ds_id, f"Det {suffix}")
    detect_run_id = _make_run(client, headers, detect_task_id)

    rem_task_id = _make_task(client, headers, "remediate", ds_id, f"Rem {suffix}")
    rem_run_id = _make_run(client, headers, rem_task_id, detect_run_id)

    val_task_id = _make_task(client, headers, "validate", ds_id, f"Val {suffix}")
    val_run_id = _make_run(client, headers, val_task_id, rem_run_id)

    qc_task_id = _make_task(client, headers, "quality_ctrl", ds_id, f"QC {suffix}")
    qc_run_id = _make_run(client, headers, qc_task_id)

    db.expire_all()
    sync_task = db.get(Task, uuid.UUID(sync_task_id))
    detect_task = db.get(Task, uuid.UUID(detect_task_id))
    rem_task = db.get(Task, uuid.UUID(rem_task_id))
    val_task = db.get(Task, uuid.UUID(val_task_id))
    qc_task = db.get(Task, uuid.UUID(qc_task_id))
    sync_run = db.get(TaskRun, uuid.UUID(sync_run_id))
    detect_run = db.get(TaskRun, uuid.UUID(detect_run_id))
    rem_run = db.get(TaskRun, uuid.UUID(rem_run_id))
    val_run = db.get(TaskRun, uuid.UUID(val_run_id))
    qc_run = db.get(TaskRun, uuid.UUID(qc_run_id))

    # DETECT run needs source_task_run_id pointing to SYNC (not settable via API)
    detect_run.source_task_run_id = sync_run.id
    # QUALITY_CTRL run needs source_task_run_id pointing to VALIDATE
    qc_run.source_task_run_id = val_run.id
    db.commit()

    # ── DataProfile (referenced by QualityControlRun.data_profile_id) ───
    dp = DataProfile(
        id=uuid.uuid4(),
        organization_id=org_id,
        task_run_id=sync_run.id,
        task_id=sync_task.id,
        data_source_id=ds_uuid,
        source_filename="test.csv",
        source_size_bytes=100,
        source_sha256="a" * 64,
        detected_encoding="utf-8",
        delimiter=",",
        row_count=10,
        column_count=3,
        duplicate_row_count=1,
        missing_value_total=2,
        column_profiles=[],
        structural_issues=[],
        limits_applied={},
    )
    db.add(dp)
    db.commit()

    # ── IssueDetectionRun (referenced by QualityControlRun.issue_detection_run_id)
    idr = IssueDetectionRun(
        organization_id=org_id,
        task_run_id=detect_run.id,
        task_id=detect_task.id,
        data_source_id=ds_uuid,
        rows_scanned=10,
        columns_scanned=3,
        total_issues_found=1,
        persisted_issue_count=1,
        issues_by_severity={"INFO": 1, "LOW": 0, "MEDIUM": 0, "HIGH": 0, "CRITICAL": 0},
        issues_by_type={"leading_whitespace": 1},
        limits_applied={},
        detection_engine_version="1.0",
        source_sha256="a" * 64,
    )
    db.add(idr)
    db.commit()

    # ── RemediationRun (referenced by ValidationRun + QualityControlRun) ─
    rem_run_row = RemediationRun(
        organization_id=org_id,
        task_run_id=rem_run.id,
        task_id=rem_task.id,
        data_source_id=ds_uuid,
        source_task_run_id=detect_run.id,
        issues_considered_count=1,
        total_changes_count=1,
        issues_skipped_count=0,
        changes_by_action={"trim_whitespace": 1},
        skipped_by_reason={},
        remediation_engine_version="1.0",
    )
    db.add(rem_run_row)
    db.commit()

    # ── ValidationRun (referenced by QualityControlRun.validation_run_id) ─
    val_run_row = ValidationRun(
        id=uuid.uuid4(), organization_id=org_id,
        task_run_id=val_run.id, task_id=val_task.id, data_source_id=ds_uuid,
        remediation_run_id=rem_run_row.id,
        validation_engine_version="1.0",
        approved_changes_considered=1,
        passed_count=1, failed_count=0, skipped_count=0,
        results_by_rule={"trim_whitespace_check": {"passed": 1}},
    )
    db.add(val_run_row)
    db.commit()

    # Give the run some timing so processing_duration_ms can be derived
    # (must set status='success' first; ck_task_runs_status_invariants enforces
    # that started_at/finished_at can only be set on terminal-status runs)
    now = datetime.now(tz=timezone.utc)
    qc_run.status = "success"
    qc_run.started_at = now - timedelta(seconds=5)
    qc_run.finished_at = now
    db.commit()

    qc_run_row = QualityControlRun(
        id=uuid.uuid4(), organization_id=org_id,
        task_run_id=qc_run.id, task_id=qc_task.id,
        data_source_id=ds_uuid,
        validation_run_id=val_run_row.id,
        remediation_run_id=rem_run_row.id,
        issue_detection_run_id=idr.id,
        data_profile_id=dp.id,
        quality_engine_version=QUALITY_ENGINE_VERSION,
        overall_score=88.5,
        release_recommendation="PASS",
        total_findings=3,
        blocking_count=0,
        warning_count=1,
        info_count=2,
        category_scores={"completeness": 90.0, "validity": 87.0},
        category_statuses={
            cat: ("passed" if i < 4 else "skipped")
            for i, cat in enumerate(QUALITY_CATEGORIES)
        },
        category_weights_used={"completeness": 1.0, "validity": 1.0},
        post_remediation_stats={
            "baseline_row_count": 10,
            "effective_row_count": 10,
            "baseline_duplicate_row_count": 1,
            "effective_duplicate_row_count": 1,
            "baseline_missing_value_total": 2,
            "effective_missing_value_total": 1,
            "addressed_issue_count": 1,
        },
        execution_snapshot={
            "snapshot_version": "1.0",
            "frozen_at": now.isoformat(),
            "inputs": {
                "validation_run_id": str(val_run_row.id),
                "validation_result_count": 1,
                "validation_result_ids_sha256": "a" * 64,
                "remediation_run_id": str(rem_run_row.id),
                "issue_detection_run_id": str(idr.id),
                "data_profile_id": str(dp.id),
                "data_profile_source_sha256": "b" * 64,
            },
            "engine": {
                "quality_engine_version": QUALITY_ENGINE_VERSION,
                "rule_versions": {},
            },
            "configuration": {
                "threshold_config_source": "org_wide",
                "threshold_config_id": None,
                "threshold_version": None,
                "fail_score_threshold": 60.0,
                "pass_score_threshold": 85.0,
                "max_validation_failure_rate": 0.0,
                "max_validation_skip_rate": 0.5,
                "max_high_severity_unresolved": 0,
                "max_critical_severity_unresolved": 0,
                "max_warnings_for_clean_pass": 0,
            },
            "categories": {
                "applicable": ["completeness", "validity"],
                "skipped": [],
                "weights_applied": {"completeness": 1.0, "validity": 1.0},
            },
        },
    )
    db.add(qc_run_row)
    db.commit()

    # Build some findings across several categories/severities
    categories = list(QUALITY_CATEGORIES)
    findings = []
    for i in range(3):
        f = QualityFinding(
            id=uuid.uuid4(), organization_id=org_id,
            quality_control_run_id=qc_run_row.id,
            category=categories[i % len(categories)],
            rule_name=f"rule_{i}",
            rule_version="1.0",
            severity=["info", "warning", "info"][i],
            outcome=["passed", "passed", "failed"][i],
            reason=f"Reason {i}",
            affected_row_count=None,
            affected_column=f"col_{i}" if i > 0 else None,
            source_issue_id=None,
            remediation_change_id=None,
            validation_result_id=None,
            quality_engine_version=QUALITY_ENGINE_VERSION,
        )
        db.add(f)
        findings.append(f)
    db.commit()

    return {
        "headers": headers,
        "org_id": org_id,
        "source_id": ds_id,
        "qc_task_id": qc_task_id,
        "qc_run_id": qc_run_id,
        "qc_run_row": qc_run_row,
        "findings": findings,
        "val_run_row": val_run_row,
        "qc_run_taskrun": qc_run,
    }



# ---------------------------------------------------------------------------
# Summary endpoint tests
# ---------------------------------------------------------------------------


class TestGetQualityControlRun:

    def test_200_with_all_required_fields(self, client, db_session):
        suffix = uuid.uuid4().hex[:8]
        sc = _build_qc_scaffold(client, db_session, suffix)
        resp = client.get(
            f"/tasks/{sc['qc_task_id']}/runs/{sc['qc_run_id']}/quality-control",
            headers=sc["headers"],
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["id"] == str(sc["qc_run_row"].id)
        assert data["task_run_id"] == sc["qc_run_id"]
        assert data["task_id"] == sc["qc_task_id"]
        assert data["overall_score"] == pytest.approx(88.5)
        assert data["release_recommendation"] == "PASS"
        assert data["total_findings"] == 3
        assert data["blocking_count"] == 0
        assert data["warning_count"] == 1
        assert data["info_count"] == 2
        assert isinstance(data["category_scores"], dict)
        assert isinstance(data["category_statuses"], dict)
        assert isinstance(data["category_weights_used"], dict)
        assert isinstance(data["post_remediation_stats"], dict)
        assert "quality_engine_version" in data
        assert "created_at" in data

    def test_processing_duration_ms_derived(self, client, db_session):
        suffix = uuid.uuid4().hex[:8]
        sc = _build_qc_scaffold(client, db_session, suffix)
        resp = client.get(
            f"/tasks/{sc['qc_task_id']}/runs/{sc['qc_run_id']}/quality-control",
            headers=sc["headers"],
        )
        assert resp.status_code == 200, resp.text
        # started_at is 5s before finished_at → ~5000 ms
        ms = resp.json()["processing_duration_ms"]
        assert ms is not None
        assert 4000 <= ms <= 6000

    def test_processing_duration_ms_none_when_not_finished(
        self, client, db_session
    ):
        suffix = uuid.uuid4().hex[:8]
        sc = _build_qc_scaffold(client, db_session, suffix)
        # Transition the run back to 'running' (finished_at=NULL) so we can
        # verify processing_duration_ms returns None.  The check constraint
        # ck_task_runs_status_invariants requires:
        #   running → started_at NOT NULL, finished_at IS NULL
        # ck_task_runs_lease_consistency requires:
        #   running → lease_token NOT NULL, lease_expires_at NOT NULL
        qc_run = sc["qc_run_taskrun"]
        db_session.expire_all()
        qc_run = db_session.get(TaskRun, qc_run.id)
        qc_run.status = "running"
        qc_run.finished_at = None
        qc_run.lease_token = uuid.uuid4()
        qc_run.lease_expires_at = datetime.now(tz=timezone.utc) + timedelta(hours=1)
        db_session.commit()

        resp = client.get(
            f"/tasks/{sc['qc_task_id']}/runs/{sc['qc_run_id']}/quality-control",
            headers=sc["headers"],
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["processing_duration_ms"] is None

    def test_404_task_not_found(self, client, db_session):
        suffix = uuid.uuid4().hex[:8]
        sc = _build_qc_scaffold(client, db_session, suffix)
        resp = client.get(
            f"/tasks/{uuid.uuid4()}/runs/{sc['qc_run_id']}/quality-control",
            headers=sc["headers"],
        )
        assert resp.status_code == 404

    def test_404_task_run_not_found(self, client, db_session):
        suffix = uuid.uuid4().hex[:8]
        sc = _build_qc_scaffold(client, db_session, suffix)
        resp = client.get(
            f"/tasks/{sc['qc_task_id']}/runs/{uuid.uuid4()}/quality-control",
            headers=sc["headers"],
        )
        assert resp.status_code == 404

    def test_404_qc_run_not_found(self, client, db_session):
        """Task + TaskRun exist but no QualityControlRun yet."""
        suffix = uuid.uuid4().hex[:8]
        headers = _register(client, suffix)
        ds_id = _create_data_source(client, headers, suffix)
        task_id = _make_task(client, headers, "quality_ctrl", ds_id, "QC")
        run_id = _make_run(client, headers, task_id)

        resp = client.get(
            f"/tasks/{task_id}/runs/{run_id}/quality-control",
            headers=headers,
        )
        assert resp.status_code == 404

    def test_404_cross_tenant(self, client, db_session):
        """Other organization's QC run returns 404 (not 403)."""
        suffix_a = uuid.uuid4().hex[:8]
        suffix_b = uuid.uuid4().hex[:8]
        sc_a = _build_qc_scaffold(client, db_session, suffix_a)
        headers_b = _register(client, suffix_b)

        resp = client.get(
            f"/tasks/{sc_a['qc_task_id']}/runs/{sc_a['qc_run_id']}/quality-control",
            headers=headers_b,
        )
        assert resp.status_code == 404

    def test_401_unauthenticated(self, client, db_session):
        suffix = uuid.uuid4().hex[:8]
        sc = _build_qc_scaffold(client, db_session, suffix)
        resp = client.get(
            f"/tasks/{sc['qc_task_id']}/runs/{sc['qc_run_id']}/quality-control",
        )
        assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Findings endpoint tests
# ---------------------------------------------------------------------------


class TestListQualityControlFindings:

    def test_200_returns_all_findings(self, client, db_session):
        suffix = uuid.uuid4().hex[:8]
        sc = _build_qc_scaffold(client, db_session, suffix)
        resp = client.get(
            f"/tasks/{sc['qc_task_id']}/runs/{sc['qc_run_id']}"
            "/quality-control/findings",
            headers=sc["headers"],
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total"] == 3
        assert len(data["items"]) == 3

    def test_finding_fields(self, client, db_session):
        suffix = uuid.uuid4().hex[:8]
        sc = _build_qc_scaffold(client, db_session, suffix)
        resp = client.get(
            f"/tasks/{sc['qc_task_id']}/runs/{sc['qc_run_id']}"
            "/quality-control/findings",
            headers=sc["headers"],
        )
        assert resp.status_code == 200, resp.text
        item = resp.json()["items"][0]
        required = {
            "id", "quality_control_run_id", "category", "rule_name",
            "rule_version", "severity", "outcome", "reason",
            "affected_row_count", "affected_column",
            "source_issue_id", "remediation_change_id", "validation_result_id",
            "quality_engine_version", "created_at",
        }
        for field in required:
            assert field in item, f"missing field {field!r}"

    def test_filter_by_category(self, client, db_session):
        suffix = uuid.uuid4().hex[:8]
        sc = _build_qc_scaffold(client, db_session, suffix)
        categories = list(QUALITY_CATEGORIES)
        # Use the first finding's category
        first_category = categories[0]
        resp = client.get(
            f"/tasks/{sc['qc_task_id']}/runs/{sc['qc_run_id']}"
            f"/quality-control/findings?category={first_category}",
            headers=sc["headers"],
        )
        assert resp.status_code == 200, resp.text
        for item in resp.json()["items"]:
            assert item["category"] == first_category

    def test_filter_by_severity(self, client, db_session):
        suffix = uuid.uuid4().hex[:8]
        sc = _build_qc_scaffold(client, db_session, suffix)
        resp = client.get(
            f"/tasks/{sc['qc_task_id']}/runs/{sc['qc_run_id']}"
            "/quality-control/findings?severity=warning",
            headers=sc["headers"],
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["severity"] == "warning"

    def test_filter_by_outcome(self, client, db_session):
        suffix = uuid.uuid4().hex[:8]
        sc = _build_qc_scaffold(client, db_session, suffix)
        resp = client.get(
            f"/tasks/{sc['qc_task_id']}/runs/{sc['qc_run_id']}"
            "/quality-control/findings?outcome=failed",
            headers=sc["headers"],
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["outcome"] == "failed"

    def test_filter_by_rule_name(self, client, db_session):
        suffix = uuid.uuid4().hex[:8]
        sc = _build_qc_scaffold(client, db_session, suffix)
        resp = client.get(
            f"/tasks/{sc['qc_task_id']}/runs/{sc['qc_run_id']}"
            "/quality-control/findings?rule_name=rule_1",
            headers=sc["headers"],
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["rule_name"] == "rule_1"

    def test_filter_by_affected_column(self, client, db_session):
        suffix = uuid.uuid4().hex[:8]
        sc = _build_qc_scaffold(client, db_session, suffix)
        resp = client.get(
            f"/tasks/{sc['qc_task_id']}/runs/{sc['qc_run_id']}"
            "/quality-control/findings?affected_column=col_1",
            headers=sc["headers"],
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["affected_column"] == "col_1"

    def test_combined_filters_and_semantics(self, client, db_session):
        """severity=info AND outcome=passed → should return the info/passed row."""
        suffix = uuid.uuid4().hex[:8]
        sc = _build_qc_scaffold(client, db_session, suffix)
        resp = client.get(
            f"/tasks/{sc['qc_task_id']}/runs/{sc['qc_run_id']}"
            "/quality-control/findings?severity=info&outcome=passed",
            headers=sc["headers"],
        )
        assert resp.status_code == 200, resp.text
        for item in resp.json()["items"]:
            assert item["severity"] == "info"
            assert item["outcome"] == "passed"

    def test_422_invalid_category(self, client, db_session):
        suffix = uuid.uuid4().hex[:8]
        sc = _build_qc_scaffold(client, db_session, suffix)
        resp = client.get(
            f"/tasks/{sc['qc_task_id']}/runs/{sc['qc_run_id']}"
            "/quality-control/findings?category=not_a_real_category",
            headers=sc["headers"],
        )
        assert resp.status_code == 422

    def test_422_invalid_severity(self, client, db_session):
        suffix = uuid.uuid4().hex[:8]
        sc = _build_qc_scaffold(client, db_session, suffix)
        resp = client.get(
            f"/tasks/{sc['qc_task_id']}/runs/{sc['qc_run_id']}"
            "/quality-control/findings?severity=critical",
            headers=sc["headers"],
        )
        assert resp.status_code == 422

    def test_422_invalid_outcome(self, client, db_session):
        suffix = uuid.uuid4().hex[:8]
        sc = _build_qc_scaffold(client, db_session, suffix)
        resp = client.get(
            f"/tasks/{sc['qc_task_id']}/runs/{sc['qc_run_id']}"
            "/quality-control/findings?outcome=unknown",
            headers=sc["headers"],
        )
        assert resp.status_code == 422

    def test_unknown_rule_name_returns_empty(self, client, db_session):
        """Unknown rule_name → empty result, not 422."""
        suffix = uuid.uuid4().hex[:8]
        sc = _build_qc_scaffold(client, db_session, suffix)
        resp = client.get(
            f"/tasks/{sc['qc_task_id']}/runs/{sc['qc_run_id']}"
            "/quality-control/findings?rule_name=nonexistent",
            headers=sc["headers"],
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_pagination_limit_and_offset(self, client, db_session):
        suffix = uuid.uuid4().hex[:8]
        sc = _build_qc_scaffold(client, db_session, suffix)
        # Limit 2 → first page
        resp1 = client.get(
            f"/tasks/{sc['qc_task_id']}/runs/{sc['qc_run_id']}"
            "/quality-control/findings?limit=2&offset=0",
            headers=sc["headers"],
        )
        assert resp1.status_code == 200, resp1.text
        data1 = resp1.json()
        assert data1["total"] == 3
        assert len(data1["items"]) == 2

        # Offset 2 → last page
        resp2 = client.get(
            f"/tasks/{sc['qc_task_id']}/runs/{sc['qc_run_id']}"
            "/quality-control/findings?limit=2&offset=2",
            headers=sc["headers"],
        )
        assert resp2.status_code == 200, resp2.text
        data2 = resp2.json()
        assert len(data2["items"]) == 1

        # No overlap between pages
        ids1 = {item["id"] for item in data1["items"]}
        ids2 = {item["id"] for item in data2["items"]}
        assert ids1.isdisjoint(ids2)

    def test_stable_sort_order(self, client, db_session):
        """Findings are returned in created_at ASC, id ASC order."""
        suffix = uuid.uuid4().hex[:8]
        sc = _build_qc_scaffold(client, db_session, suffix)
        resp = client.get(
            f"/tasks/{sc['qc_task_id']}/runs/{sc['qc_run_id']}"
            "/quality-control/findings",
            headers=sc["headers"],
        )
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        # Verify there is no reversal: created_at should be non-descending
        created_ats = [item["created_at"] for item in items]
        assert created_ats == sorted(created_ats)

    def test_empty_when_no_findings(self, client, db_session):
        """QualityControlRun with zero findings → total=0, items=[]."""
        suffix = uuid.uuid4().hex[:8]
        sc = _build_qc_scaffold(client, db_session, suffix)
        # Delete all findings for this run
        findings = sc["findings"]
        for f in findings:
            db_session.delete(f)
        db_session.commit()

        resp = client.get(
            f"/tasks/{sc['qc_task_id']}/runs/{sc['qc_run_id']}"
            "/quality-control/findings",
            headers=sc["headers"],
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["total"] == 0
        assert data["items"] == []

    def test_404_cross_tenant_findings(self, client, db_session):
        suffix_a = uuid.uuid4().hex[:8]
        suffix_b = uuid.uuid4().hex[:8]
        sc_a = _build_qc_scaffold(client, db_session, suffix_a)
        headers_b = _register(client, suffix_b)
        resp = client.get(
            f"/tasks/{sc_a['qc_task_id']}/runs/{sc_a['qc_run_id']}"
            "/quality-control/findings",
            headers=headers_b,
        )
        assert resp.status_code == 404

    def test_404_findings_run_not_found(self, client, db_session):
        suffix = uuid.uuid4().hex[:8]
        sc = _build_qc_scaffold(client, db_session, suffix)
        resp = client.get(
            f"/tasks/{sc['qc_task_id']}/runs/{uuid.uuid4()}"
            "/quality-control/findings",
            headers=sc["headers"],
        )
        assert resp.status_code == 404
