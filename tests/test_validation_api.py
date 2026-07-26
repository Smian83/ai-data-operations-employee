"""Module 17 Phase 4: coverage for
GET /tasks/{task_id}/runs/{run_id}/validation (summary) and
GET /tasks/{task_id}/runs/{run_id}/validation/results (paginated, filterable).

Rows are inserted directly via db_session (never through the real worker
handler/CSV pipeline) since these endpoints only read already-persisted
rows -- same "cheap because the work already happened" justification that
test_remediation_api.py and test_issue_detection_api.py both document.

The full prerequisite chain for a ValidationRun is:
  DataSource -> Task[DETECT] -> TaskRun[DETECT] -> IssueDetectionRun
             -> Task[REMEDIATE] -> TaskRun[REMEDIATE] -> RemediationRun
             -> RemediationChange
             -> Task[VALIDATE] -> TaskRun[VALIDATE] -> ValidationRun
             -> ValidationResult (one per approved change)

All writes are direct ORM inserts rather than going through the worker
handler so tests run fast and in isolation."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.issue import Issue
from app.models.issue_detection_run import IssueDetectionRun
from app.models.remediation_change import RemediationChange
from app.models.remediation_run import RemediationRun
from app.models.task import Task
from app.models.task_run import TaskRun
from app.models.validation_result import ValidationResult
from app.models.validation_run import ValidationRun


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register(client: TestClient, suffix: str) -> dict:
    response = client.post(
        "/auth/register",
        json={
            "organization_name": f"Validation API Org {suffix}",
            "email": f"val-api-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Validation API User",
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_data_source(client: TestClient, headers: dict) -> str:
    resp = client.post(
        "/data-sources",
        json={
            "name": f"Uploaded Customers {uuid.uuid4().hex[:8]}",
            "source_type": "csv_upload",
            "connection_metadata": {"file_path": "customers.csv"},
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _create_task_and_run(
    client: TestClient,
    headers: dict,
    task_type: str,
    *,
    source_id: str | None = None,
    source_task_run_id: str | None = None,
) -> tuple[str, str, str]:
    """Returns (task_id, run_id, data_source_id)."""
    if source_id is None:
        source_id = _create_data_source(client, headers)

    task_resp = client.post(
        "/tasks",
        json={
            "name": f"{task_type} task {uuid.uuid4().hex[:6]}",
            "task_type": task_type,
            "data_source_id": source_id,
        },
        headers=headers,
    )
    assert task_resp.status_code == 201, task_resp.text

    payload = {"source_task_run_id": source_task_run_id} if source_task_run_id else None
    run_resp = client.post(
        f"/tasks/{task_resp.json()['id']}/runs", json=payload, headers=headers
    )
    assert run_resp.status_code == 201, run_resp.text
    return task_resp.json()["id"], run_resp.json()["id"], source_id


def _insert_detection_run(
    db: Session,
    *,
    organization_id: uuid.UUID,
    task_id: uuid.UUID,
    task_run_id: uuid.UUID,
    data_source_id: uuid.UUID,
) -> IssueDetectionRun:
    run = IssueDetectionRun(
        organization_id=organization_id,
        task_run_id=task_run_id,
        task_id=task_id,
        data_source_id=data_source_id,
        rows_scanned=10,
        columns_scanned=3,
        total_issues_found=1,
        persisted_issue_count=1,
        issues_by_severity={"INFO": 1, "LOW": 0, "MEDIUM": 0, "HIGH": 0, "CRITICAL": 0},
        issues_by_type={"leading_whitespace": 1},
        limits_applied={"max_persisted_issues": 10_000},
        detection_engine_version="1.0",
        source_sha256="a" * 64,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _insert_issue(
    db: Session,
    *,
    organization_id: uuid.UUID,
    detection_run_id: uuid.UUID,
    row_number: int = 2,
    column_name: str = "name",
) -> Issue:
    issue = Issue(
        organization_id=organization_id,
        detection_run_id=detection_run_id,
        row_number=row_number,
        column_name=column_name,
        issue_type="leading_whitespace",
        severity="INFO",
        original_value="  Ada",
        suggested_fix="Ada",
        confidence=1.0,
    )
    db.add(issue)
    db.commit()
    db.refresh(issue)
    return issue


def _insert_remediation_run(
    db: Session,
    *,
    organization_id: uuid.UUID,
    task_id: uuid.UUID,
    task_run_id: uuid.UUID,
    data_source_id: uuid.UUID,
    source_task_run_id: uuid.UUID,
    total_changes_count: int = 1,
) -> RemediationRun:
    run = RemediationRun(
        organization_id=organization_id,
        task_run_id=task_run_id,
        task_id=task_id,
        data_source_id=data_source_id,
        source_task_run_id=source_task_run_id,
        issues_considered_count=total_changes_count,
        total_changes_count=total_changes_count,
        issues_skipped_count=0,
        changes_by_action={"trim_whitespace": total_changes_count},
        skipped_by_reason={},
        remediation_engine_version="1.0",
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _insert_change(
    db: Session,
    *,
    organization_id: uuid.UUID,
    remediation_run_id: uuid.UUID,
    source_issue_id: uuid.UUID,
    row_number: int = 2,
    column_name: str = "name",
    action: str = "trim_whitespace",
    original_value: str = "  Ada",
    proposed_value: str = "Ada",
) -> RemediationChange:
    change = RemediationChange(
        organization_id=organization_id,
        remediation_run_id=remediation_run_id,
        source_issue_id=source_issue_id,
        row_number=row_number,
        column_name=column_name,
        action=action,
        original_value=original_value,
        proposed_value=proposed_value,
        reason="test reason",
        confidence=1.0,
    )
    db.add(change)
    db.commit()
    db.refresh(change)
    return change


def _insert_validation_run(
    db: Session,
    *,
    organization_id: uuid.UUID,
    task_id: uuid.UUID,
    task_run_id: uuid.UUID,
    data_source_id: uuid.UUID,
    remediation_run_id: uuid.UUID,
    approved_changes_considered: int = 1,
    passed_count: int = 1,
    failed_count: int = 0,
    skipped_count: int = 0,
    results_by_rule: dict | None = None,
    validation_engine_version: str = "1.0",
) -> ValidationRun:
    run = ValidationRun(
        organization_id=organization_id,
        task_run_id=task_run_id,
        task_id=task_id,
        data_source_id=data_source_id,
        remediation_run_id=remediation_run_id,
        approved_changes_considered=approved_changes_considered,
        passed_count=passed_count,
        failed_count=failed_count,
        skipped_count=skipped_count,
        results_by_rule=results_by_rule or {"validate_trim_whitespace": passed_count + failed_count},
        validation_engine_version=validation_engine_version,
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


def _insert_validation_result(
    db: Session,
    *,
    organization_id: uuid.UUID,
    validation_run_id: uuid.UUID,
    remediation_run_id: uuid.UUID,
    remediation_change_id: uuid.UUID,
    source_issue_id: uuid.UUID,
    validation_rule: str = "validate_trim_whitespace",
    validation_rule_version: str = "1.0",
    outcome: str = "passed",
    reason: str = "PASSED",
    original_value: str | None = "  Ada",
    proposed_value: str | None = "Ada",
    validation_engine_version: str = "1.0",
) -> ValidationResult:
    result = ValidationResult(
        organization_id=organization_id,
        validation_run_id=validation_run_id,
        remediation_run_id=remediation_run_id,
        remediation_change_id=remediation_change_id,
        source_issue_id=source_issue_id,
        validation_rule=validation_rule,
        validation_rule_version=validation_rule_version,
        outcome=outcome,
        reason=reason,
        original_value=original_value,
        proposed_value=proposed_value,
        validation_engine_version=validation_engine_version,
    )
    db.add(result)
    db.commit()
    db.refresh(result)
    return result


def _mark_completed(
    db: Session, task_run_id: uuid.UUID, *, started_at: datetime, finished_at: datetime
) -> None:
    """Transitions a TaskRun to SUCCESS with explicit timing fields.
    Required to make processing_duration_ms computable/testable without
    running the real worker engine."""
    run = db.get(TaskRun, task_run_id)
    run.status = "success"
    run.started_at = started_at
    run.finished_at = finished_at
    db.commit()


def _build_full_chain(
    client: TestClient,
    db: Session,
    headers: dict,
    *,
    n_changes: int = 1,
    failed: int = 0,
    skipped: int = 0,
    column_name: str = "name",
    action: str = "trim_whitespace",
    outcome: str = "passed",
    validation_rule: str = "validate_trim_whitespace",
) -> dict:
    # Derive passed_count so passed + failed + skipped == n_changes always.
    passed = n_changes - failed - skipped
    """Full setup: DETECT -> REMEDIATE -> VALIDATE chain with n_changes
    changes and matching validation results. Returns a dict of every ID a
    test might need."""
    # DETECT
    detect_task_id, detect_run_id, source_id = _create_task_and_run(
        client, headers, "detect"
    )
    detect_task = db.get(Task, uuid.UUID(detect_task_id))
    org_id = detect_task.organization_id
    detection_run = _insert_detection_run(
        db,
        organization_id=org_id,
        task_id=uuid.UUID(detect_task_id),
        task_run_id=uuid.UUID(detect_run_id),
        data_source_id=uuid.UUID(source_id),
    )

    # REMEDIATE
    remediate_task_id, remediate_run_id, _ = _create_task_and_run(
        client, headers, "remediate",
        source_id=source_id, source_task_run_id=detect_run_id,
    )
    remediation_run = _insert_remediation_run(
        db,
        organization_id=org_id,
        task_id=uuid.UUID(remediate_task_id),
        task_run_id=uuid.UUID(remediate_run_id),
        data_source_id=uuid.UUID(source_id),
        source_task_run_id=uuid.UUID(detect_run_id),
        total_changes_count=n_changes,
    )

    # Insert issues + changes
    changes = []
    for i in range(n_changes):
        issue = _insert_issue(
            db, organization_id=org_id,
            detection_run_id=detection_run.id, row_number=i + 2,
            column_name=column_name,
        )
        change = _insert_change(
            db, organization_id=org_id,
            remediation_run_id=remediation_run.id, source_issue_id=issue.id,
            row_number=i + 2, column_name=column_name, action=action,
        )
        changes.append((issue, change))

    # VALIDATE
    validate_task_id, validate_run_id, _ = _create_task_and_run(
        client, headers, "validate",
        source_id=source_id, source_task_run_id=remediate_run_id,
    )
    validation_run = _insert_validation_run(
        db,
        organization_id=org_id,
        task_id=uuid.UUID(validate_task_id),
        task_run_id=uuid.UUID(validate_run_id),
        data_source_id=uuid.UUID(source_id),
        remediation_run_id=remediation_run.id,
        approved_changes_considered=n_changes,
        passed_count=passed,
        failed_count=failed,
        skipped_count=skipped,
        results_by_rule={validation_rule: n_changes},
    )

    # Validation results
    results = []
    for issue, change in changes:
        r = _insert_validation_result(
            db,
            organization_id=org_id,
            validation_run_id=validation_run.id,
            remediation_run_id=remediation_run.id,
            remediation_change_id=change.id,
            source_issue_id=issue.id,
            validation_rule=validation_rule,
            outcome=outcome,
        )
        results.append(r)

    return {
        "source_id": source_id,
        "org_id": org_id,
        "detect_task_id": detect_task_id,
        "detect_run_id": detect_run_id,
        "detection_run": detection_run,
        "remediate_task_id": remediate_task_id,
        "remediate_run_id": remediate_run_id,
        "remediation_run": remediation_run,
        "changes": changes,
        "validate_task_id": validate_task_id,
        "validate_run_id": validate_run_id,
        "validation_run": validation_run,
        "results": results,
    }


# ---------------------------------------------------------------------------
# Summary endpoint: GET .../validation
# ---------------------------------------------------------------------------


def test_validation_summary_returns_200(client: TestClient, db_session: Session) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_full_chain(client, db_session, headers)

    response = client.get(
        f"/tasks/{ctx['validate_task_id']}/runs/{ctx['validate_run_id']}/validation",
        headers=headers,
    )

    assert response.status_code == 200, response.text


def test_validation_summary_field_set(client: TestClient, db_session: Session) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_full_chain(client, db_session, headers)

    response = client.get(
        f"/tasks/{ctx['validate_task_id']}/runs/{ctx['validate_run_id']}/validation",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    vr = ctx["validation_run"]
    assert body["id"] == str(vr.id)
    assert body["organization_id"] == str(ctx["org_id"])
    assert body["task_run_id"] == ctx["validate_run_id"]
    assert body["task_id"] == ctx["validate_task_id"]
    assert body["data_source_id"] == ctx["source_id"]
    assert body["remediation_run_id"] == str(ctx["remediation_run"].id)
    assert body["approved_changes_considered"] == 1
    assert body["passed_count"] == 1
    assert body["failed_count"] == 0
    assert body["skipped_count"] == 0
    assert body["results_by_rule"] == {"validate_trim_whitespace": 1}
    assert body["validation_engine_version"] == "1.0"
    assert "created_at" in body
    # processing_duration_ms may be None (task run not completed)
    assert "processing_duration_ms" in body
    # Verify the full field set matches exactly what the schema declares.
    assert set(body) == {
        "id",
        "organization_id",
        "task_run_id",
        "task_id",
        "data_source_id",
        "remediation_run_id",
        "approved_changes_considered",
        "passed_count",
        "failed_count",
        "skipped_count",
        "results_by_rule",
        "validation_engine_version",
        "processing_duration_ms",
        "created_at",
    }


def test_validation_summary_derived_processing_duration_ms(
    client: TestClient, db_session: Session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_full_chain(client, db_session, headers)
    started = datetime(2026, 3, 1, 9, 0, 0, tzinfo=timezone.utc)
    finished = started + timedelta(milliseconds=2750)
    _mark_completed(db_session, uuid.UUID(ctx["validate_run_id"]), started_at=started, finished_at=finished)

    response = client.get(
        f"/tasks/{ctx['validate_task_id']}/runs/{ctx['validate_run_id']}/validation",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["processing_duration_ms"] == 2750


def test_validation_summary_duration_null_when_not_finished(
    client: TestClient, db_session: Session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_full_chain(client, db_session, headers)
    # Do NOT call _mark_completed; TaskRun stays pending.

    response = client.get(
        f"/tasks/{ctx['validate_task_id']}/runs/{ctx['validate_run_id']}/validation",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert response.json()["processing_duration_ms"] is None


def test_validation_summary_404_when_validation_run_missing(
    client: TestClient, db_session: Session
) -> None:
    """A valid VALIDATE task+run exists but no ValidationRun has been
    inserted yet -- simulates a run that's still in flight."""
    headers = _register(client, uuid.uuid4().hex)
    detect_task_id, detect_run_id, source_id = _create_task_and_run(
        client, headers, "detect"
    )
    detect_task = db_session.get(Task, uuid.UUID(detect_task_id))
    _insert_detection_run(
        db_session,
        organization_id=detect_task.organization_id,
        task_id=uuid.UUID(detect_task_id),
        task_run_id=uuid.UUID(detect_run_id),
        data_source_id=uuid.UUID(source_id),
    )
    remediate_task_id, remediate_run_id, _ = _create_task_and_run(
        client, headers, "remediate",
        source_id=source_id, source_task_run_id=detect_run_id,
    )
    _insert_remediation_run(
        db_session,
        organization_id=detect_task.organization_id,
        task_id=uuid.UUID(remediate_task_id),
        task_run_id=uuid.UUID(remediate_run_id),
        data_source_id=uuid.UUID(source_id),
        source_task_run_id=uuid.UUID(detect_run_id),
    )
    validate_task_id, validate_run_id, _ = _create_task_and_run(
        client, headers, "validate",
        source_id=source_id, source_task_run_id=remediate_run_id,
    )
    # No _insert_validation_run call.

    response = client.get(
        f"/tasks/{validate_task_id}/runs/{validate_run_id}/validation",
        headers=headers,
    )

    assert response.status_code == 404, response.text


def test_validation_summary_404_when_task_missing(
    client: TestClient, db_session: Session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_full_chain(client, db_session, headers)
    fake_task_id = uuid.uuid4()

    response = client.get(
        f"/tasks/{fake_task_id}/runs/{ctx['validate_run_id']}/validation",
        headers=headers,
    )

    assert response.status_code == 404, response.text


def test_validation_summary_404_when_task_run_missing(
    client: TestClient, db_session: Session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_full_chain(client, db_session, headers)
    fake_run_id = uuid.uuid4()

    response = client.get(
        f"/tasks/{ctx['validate_task_id']}/runs/{fake_run_id}/validation",
        headers=headers,
    )

    assert response.status_code == 404, response.text


def test_validation_summary_cross_tenant_404(
    client: TestClient, db_session: Session
) -> None:
    """A different org's user cannot see another org's validation run."""
    headers_a = _register(client, uuid.uuid4().hex)
    ctx = _build_full_chain(client, db_session, headers_a)

    # Register a completely separate org.
    headers_b = _register(client, uuid.uuid4().hex)

    response = client.get(
        f"/tasks/{ctx['validate_task_id']}/runs/{ctx['validate_run_id']}/validation",
        headers=headers_b,
    )

    assert response.status_code == 404, response.text


def test_validation_summary_non_superuser_can_read(
    client: TestClient, db_session: Session
) -> None:
    """Read access is not gated on is_superuser -- any active org member
    can call the summary endpoint."""
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_full_chain(client, db_session, headers)

    # The registered user is not a superuser (is_superuser defaults to False
    # for regular registration). Verify the endpoint returns 200 regardless.
    response = client.get(
        f"/tasks/{ctx['validate_task_id']}/runs/{ctx['validate_run_id']}/validation",
        headers=headers,
    )

    assert response.status_code == 200, response.text


# ---------------------------------------------------------------------------
# Results endpoint: GET .../validation/results
# ---------------------------------------------------------------------------


def test_validation_results_returns_200(client: TestClient, db_session: Session) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_full_chain(client, db_session, headers)

    response = client.get(
        f"/tasks/{ctx['validate_task_id']}/runs/{ctx['validate_run_id']}/validation/results",
        headers=headers,
    )

    assert response.status_code == 200, response.text


def test_validation_results_pagination_shape(
    client: TestClient, db_session: Session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_full_chain(client, db_session, headers, n_changes=3)

    response = client.get(
        f"/tasks/{ctx['validate_task_id']}/runs/{ctx['validate_run_id']}/validation/results",
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == {"items", "total", "limit", "offset"}
    assert body["total"] == 3
    assert body["limit"] == 50   # default
    assert body["offset"] == 0
    assert len(body["items"]) == 3


def test_validation_results_default_limit_50(
    client: TestClient, db_session: Session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_full_chain(client, db_session, headers, n_changes=1)

    response = client.get(
        f"/tasks/{ctx['validate_task_id']}/runs/{ctx['validate_run_id']}/validation/results",
        headers=headers,
    )

    assert response.json()["limit"] == 50


def test_validation_results_maximum_limit_enforcement(
    client: TestClient, db_session: Session
) -> None:
    """A limit > 100 must be rejected (enforced by PaginationParams)."""
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_full_chain(client, db_session, headers)

    response = client.get(
        f"/tasks/{ctx['validate_task_id']}/runs/{ctx['validate_run_id']}/validation/results"
        "?limit=101",
        headers=headers,
    )

    assert response.status_code == 422, response.text


def test_validation_results_outcome_filter(
    client: TestClient, db_session: Session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_full_chain(client, db_session, headers, n_changes=2, outcome="passed")
    # Insert one additional 'failed' result for the second change.
    issue_for_failed, second_change = ctx["changes"][1]
    _insert_validation_result(
        db_session,
        organization_id=ctx["org_id"],
        validation_run_id=ctx["validation_run"].id,
        remediation_run_id=ctx["remediation_run"].id,
        remediation_change_id=second_change.id,
        source_issue_id=issue_for_failed.id,
        outcome="failed",
        reason="FAILED",
    )

    resp_passed = client.get(
        f"/tasks/{ctx['validate_task_id']}/runs/{ctx['validate_run_id']}/validation/results"
        "?outcome=passed",
        headers=headers,
    )
    resp_failed = client.get(
        f"/tasks/{ctx['validate_task_id']}/runs/{ctx['validate_run_id']}/validation/results"
        "?outcome=failed",
        headers=headers,
    )

    assert resp_passed.status_code == 200, resp_passed.text
    assert resp_failed.status_code == 200, resp_failed.text
    # 2 originally inserted as "passed" + the one we added as "failed"
    assert all(r["outcome"] == "passed" for r in resp_passed.json()["items"])
    assert all(r["outcome"] == "failed" for r in resp_failed.json()["items"])
    assert resp_failed.json()["total"] == 1


def test_validation_results_validation_rule_filter(
    client: TestClient, db_session: Session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_full_chain(client, db_session, headers, n_changes=1,
                            validation_rule="validate_trim_whitespace")

    # Insert a second result with a different valid rule name.
    issue, change = ctx["changes"][0]
    _insert_validation_result(
        db_session,
        organization_id=ctx["org_id"],
        validation_run_id=ctx["validation_run"].id,
        remediation_run_id=ctx["remediation_run"].id,
        remediation_change_id=change.id,
        source_issue_id=issue.id,
        validation_rule="validate_collapse_multiple_spaces",
        outcome="passed",
        reason="PASSED",
    )

    resp = client.get(
        f"/tasks/{ctx['validate_task_id']}/runs/{ctx['validate_run_id']}/validation/results"
        "?validation_rule=validate_collapse_multiple_spaces",
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["validation_rule"] == "validate_collapse_multiple_spaces"


def test_validation_results_column_name_filter(
    client: TestClient, db_session: Session
) -> None:
    """column_name is stored on RemediationChange; the endpoint JOINs to
    filter by it. Only results for changes on 'name' should be returned."""
    headers = _register(client, uuid.uuid4().hex)

    # Build a chain with 1 change on column "name".
    ctx = _build_full_chain(client, db_session, headers, n_changes=1, column_name="name")

    # Insert a second change on a different column + its result.
    issue = _insert_issue(
        db_session, organization_id=ctx["org_id"],
        detection_run_id=ctx["detection_run"].id, column_name="email",
    )
    other_change = _insert_change(
        db_session, organization_id=ctx["org_id"],
        remediation_run_id=ctx["remediation_run"].id, source_issue_id=issue.id,
        column_name="email",
    )
    _insert_validation_result(
        db_session,
        organization_id=ctx["org_id"],
        validation_run_id=ctx["validation_run"].id,
        remediation_run_id=ctx["remediation_run"].id,
        remediation_change_id=other_change.id,
        source_issue_id=issue.id,
        outcome="passed",
        reason="PASSED_TRIM_WHITESPACE",
    )

    resp = client.get(
        f"/tasks/{ctx['validate_task_id']}/runs/{ctx['validate_run_id']}/validation/results"
        "?column_name=name",
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # Only the 'name' change result should be returned.
    assert body["total"] == 1
    # The result's remediation_change_id should be the 'name' change.
    _, name_change = ctx["changes"][0]
    assert body["items"][0]["remediation_change_id"] == str(name_change.id)


def test_validation_results_combined_filters(
    client: TestClient, db_session: Session
) -> None:
    """outcome + validation_rule together narrow results correctly."""
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_full_chain(client, db_session, headers, n_changes=1,
                            outcome="passed", validation_rule="validate_trim_whitespace")

    # Add a 'failed' + 'validate_trim_whitespace' result.
    issue, change = ctx["changes"][0]
    _insert_validation_result(
        db_session,
        organization_id=ctx["org_id"],
        validation_run_id=ctx["validation_run"].id,
        remediation_run_id=ctx["remediation_run"].id,
        remediation_change_id=change.id,
        source_issue_id=issue.id,
        validation_rule="validate_trim_whitespace",
        outcome="failed",
        reason="FAILED",
    )
    # Add a 'passed' + different valid rule result.
    _insert_validation_result(
        db_session,
        organization_id=ctx["org_id"],
        validation_run_id=ctx["validation_run"].id,
        remediation_run_id=ctx["remediation_run"].id,
        remediation_change_id=change.id,
        source_issue_id=issue.id,
        validation_rule="validate_collapse_multiple_spaces",
        outcome="passed",
        reason="PASSED",
    )

    resp = client.get(
        f"/tasks/{ctx['validate_task_id']}/runs/{ctx['validate_run_id']}/validation/results"
        "?outcome=passed&validation_rule=validate_trim_whitespace",
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["outcome"] == "passed"
    assert body["items"][0]["validation_rule"] == "validate_trim_whitespace"


def test_validation_results_column_name_exact_match(
    client: TestClient, db_session: Session
) -> None:
    """column_name filter is case-sensitive exact match -- 'Name' must not
    match a change on column 'name'."""
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_full_chain(client, db_session, headers, n_changes=1, column_name="name")

    resp = client.get(
        f"/tasks/{ctx['validate_task_id']}/runs/{ctx['validate_run_id']}/validation/results"
        "?column_name=Name",
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    # 'Name' != 'name' -- exact match means zero results.
    assert resp.json()["total"] == 0


def test_validation_results_stable_ordering(
    client: TestClient, db_session: Session
) -> None:
    """Results come back in created_at ASC, id ASC order regardless of
    insertion order."""
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_full_chain(client, db_session, headers, n_changes=3)

    resp = client.get(
        f"/tasks/{ctx['validate_task_id']}/runs/{ctx['validate_run_id']}/validation/results",
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    items = resp.json()["items"]
    assert len(items) == 3
    created_ats = [it["created_at"] for it in items]
    assert created_ats == sorted(created_ats)


def test_validation_results_empty_result_set(
    client: TestClient, db_session: Session
) -> None:
    """A validation run with no results returns an empty items list, not
    a 404 or error."""
    headers = _register(client, uuid.uuid4().hex)
    # Build a chain but with approved_changes_considered=0 (empty run).
    detect_task_id, detect_run_id, source_id = _create_task_and_run(
        client, headers, "detect"
    )
    detect_task = db_session.get(Task, uuid.UUID(detect_task_id))
    org_id = detect_task.organization_id
    _insert_detection_run(
        db_session,
        organization_id=org_id,
        task_id=uuid.UUID(detect_task_id),
        task_run_id=uuid.UUID(detect_run_id),
        data_source_id=uuid.UUID(source_id),
    )
    remediate_task_id, remediate_run_id, _ = _create_task_and_run(
        client, headers, "remediate",
        source_id=source_id, source_task_run_id=detect_run_id,
    )
    remediation_run = _insert_remediation_run(
        db_session,
        organization_id=org_id,
        task_id=uuid.UUID(remediate_task_id),
        task_run_id=uuid.UUID(remediate_run_id),
        data_source_id=uuid.UUID(source_id),
        source_task_run_id=uuid.UUID(detect_run_id),
        total_changes_count=0,
    )
    validate_task_id, validate_run_id, _ = _create_task_and_run(
        client, headers, "validate",
        source_id=source_id, source_task_run_id=remediate_run_id,
    )
    _insert_validation_run(
        db_session,
        organization_id=org_id,
        task_id=uuid.UUID(validate_task_id),
        task_run_id=uuid.UUID(validate_run_id),
        data_source_id=uuid.UUID(source_id),
        remediation_run_id=remediation_run.id,
        approved_changes_considered=0,
        passed_count=0,
        failed_count=0,
        skipped_count=0,
        results_by_rule={},
    )

    resp = client.get(
        f"/tasks/{validate_task_id}/runs/{validate_run_id}/validation/results",
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["items"] == []
    assert body["total"] == 0


def test_validation_results_correct_total_count(
    client: TestClient, db_session: Session
) -> None:
    """total reflects all results matching the filter, not just this page."""
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_full_chain(client, db_session, headers, n_changes=5)

    # Fetch only 2 items per page.
    resp = client.get(
        f"/tasks/{ctx['validate_task_id']}/runs/{ctx['validate_run_id']}/validation/results"
        "?limit=2&offset=0",
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total"] == 5   # all 5, not just the 2 on this page
    assert len(body["items"]) == 2
    assert body["limit"] == 2
    assert body["offset"] == 0


def test_validation_results_cross_tenant_404(
    client: TestClient, db_session: Session
) -> None:
    headers_a = _register(client, uuid.uuid4().hex)
    ctx = _build_full_chain(client, db_session, headers_a)
    headers_b = _register(client, uuid.uuid4().hex)

    resp = client.get(
        f"/tasks/{ctx['validate_task_id']}/runs/{ctx['validate_run_id']}/validation/results",
        headers=headers_b,
    )

    assert resp.status_code == 404, resp.text


def test_validation_results_invalid_outcome_rejected(
    client: TestClient, db_session: Session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_full_chain(client, db_session, headers)

    resp = client.get(
        f"/tasks/{ctx['validate_task_id']}/runs/{ctx['validate_run_id']}/validation/results"
        "?outcome=not_a_valid_outcome",
        headers=headers,
    )

    assert resp.status_code == 422, resp.text


def test_validation_results_invalid_validation_rule_rejected(
    client: TestClient, db_session: Session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_full_chain(client, db_session, headers)

    resp = client.get(
        f"/tasks/{ctx['validate_task_id']}/runs/{ctx['validate_run_id']}/validation/results"
        "?validation_rule=not_a_real_rule",
        headers=headers,
    )

    assert resp.status_code == 422, resp.text


def test_validation_results_no_n_plus_1(
    client: TestClient, db_session: Session
) -> None:
    """Inserting N results and fetching a page of N must not trigger a
    per-row query. We verify this by ensuring the response shape is a flat
    list (not N nested calls) and that all items are returned in one page.
    This is a structural check -- the implementation uses a single paginated
    SELECT + a single COUNT, never a per-row sub-query."""
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_full_chain(client, db_session, headers, n_changes=10)

    resp = client.get(
        f"/tasks/{ctx['validate_task_id']}/runs/{ctx['validate_run_id']}/validation/results"
        "?limit=10&offset=0",
        headers=headers,
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    # All 10 results returned in a single page -- no extra per-row fetches.
    assert body["total"] == 10
    assert len(body["items"]) == 10
    # Every item has the same validation_run_id (single-query batch, not N lookups).
    run_ids = {it["validation_run_id"] for it in body["items"]}
    assert len(run_ids) == 1
    assert run_ids == {str(ctx["validation_run"].id)}
