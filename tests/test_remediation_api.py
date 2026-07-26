"""Module 15 Phase 4: coverage for
GET /tasks/{task_id}/runs/{run_id}/remediation (summary) and
GET /tasks/{task_id}/runs/{run_id}/remediation/changes (paginated,
filterable detail). Mirrors test_issue_detection_api.py's conventions
exactly: rows are inserted directly via db_session (never through the
real worker handler/CSV pipeline) since these endpoints only read
already-persisted rows -- same "cheap because the work already happened"
justification Module 14 Phase 3's own test file documents."""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.issue import Issue
from app.models.issue_detection_run import IssueDetectionRun
from app.models.remediation_change import RemediationChange
from app.models.remediation_run import RemediationRun
from app.models.task import Task
from app.models.task_run import TaskRun


def _register(client: TestClient, suffix: str) -> dict:
    response = client.post(
        "/auth/register",
        json={
            "organization_name": f"Remediate API Org {suffix}",
            "email": f"remediate-api-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Remediate API User",
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_data_source(client: TestClient, headers: dict) -> str:
    source_response = client.post(
        "/data-sources",
        json={
            "name": "Uploaded Customers",
            "source_type": "csv_upload",
            "connection_metadata": {"file_path": "customers.csv"},
        },
        headers=headers,
    )
    assert source_response.status_code == 201, source_response.text
    return source_response.json()["id"]


def _create_task_and_run(
    client: TestClient,
    headers: dict,
    task_type: str,
    *,
    source_id: str | None = None,
    source_task_run_id: str | None = None,
) -> tuple[str, str, str]:
    """Returns (task_id, run_id, data_source_id). Creates a fresh data
    source when source_id is omitted; pass an existing one to chain a
    second task (e.g. REMEDIATE) onto the same data source a prior DETECT
    task already used, since data source names are unique per org."""
    if source_id is None:
        source_id = _create_data_source(client, headers)

    task_response = client.post(
        "/tasks",
        json={"name": f"{task_type} task", "task_type": task_type, "data_source_id": source_id},
        headers=headers,
    )
    assert task_response.status_code == 201, task_response.text

    payload = {"source_task_run_id": source_task_run_id} if source_task_run_id else None
    run_response = client.post(
        f"/tasks/{task_response.json()['id']}/runs", json=payload, headers=headers
    )
    assert run_response.status_code == 201, run_response.text
    return task_response.json()["id"], run_response.json()["id"], source_id


def _insert_detection_run(
    db_session, *, organization_id, task_id, task_run_id, data_source_id, source_sha256="a" * 64
) -> IssueDetectionRun:
    run = IssueDetectionRun(
        organization_id=organization_id,
        task_run_id=task_run_id,
        task_id=task_id,
        data_source_id=data_source_id,
        rows_scanned=10,
        columns_scanned=3,
        total_issues_found=0,
        persisted_issue_count=0,
        issues_by_severity={"INFO": 0, "LOW": 0, "MEDIUM": 0, "HIGH": 0, "CRITICAL": 0},
        issues_by_type={},
        limits_applied={"max_persisted_issues": 10_000},
        detection_engine_version="1.0",
        source_sha256=source_sha256,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


def _insert_issue(db_session, *, organization_id, detection_run_id, row_number=2) -> Issue:
    issue = Issue(
        organization_id=organization_id,
        detection_run_id=detection_run_id,
        row_number=row_number,
        column_name="name",
        issue_type="leading_whitespace",
        severity="INFO",
        original_value="  Ada",
        suggested_fix="Ada",
        confidence=1.0,
    )
    db_session.add(issue)
    db_session.commit()
    db_session.refresh(issue)
    return issue


def _insert_remediation_run(
    db_session,
    *,
    organization_id,
    task_id,
    task_run_id,
    data_source_id,
    source_task_run_id,
    issues_considered_count=0,
    total_changes_count=0,
    issues_skipped_count=0,
    changes_by_action=None,
    skipped_by_reason=None,
) -> RemediationRun:
    run = RemediationRun(
        organization_id=organization_id,
        task_run_id=task_run_id,
        task_id=task_id,
        data_source_id=data_source_id,
        source_task_run_id=source_task_run_id,
        issues_considered_count=issues_considered_count,
        total_changes_count=total_changes_count,
        issues_skipped_count=issues_skipped_count,
        changes_by_action=changes_by_action or {},
        skipped_by_reason=skipped_by_reason or {},
        remediation_engine_version="1.0",
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


def _insert_change(
    db_session,
    *,
    organization_id,
    remediation_run_id,
    source_issue_id,
    row_number,
    column_name,
    action,
    proposed_value="clean",
) -> RemediationChange:
    change = RemediationChange(
        organization_id=organization_id,
        remediation_run_id=remediation_run_id,
        source_issue_id=source_issue_id,
        row_number=row_number,
        column_name=column_name,
        action=action,
        original_value="dirty",
        proposed_value=proposed_value,
        reason="test reason",
        confidence=1.0,
    )
    db_session.add(change)
    db_session.commit()
    db_session.refresh(change)
    return change


def _mark_completed(db_session, task_run_id, *, started_at, finished_at) -> None:
    """Directly transitions a TaskRun to SUCCESS with explicit
    started_at/finished_at -- satisfies ck_task_runs_status_invariants,
    which requires all three set together. Used only to make
    processing_duration_ms computable/testable without running the real
    worker engine."""
    task_run = db_session.get(TaskRun, task_run_id)
    task_run.status = "success"
    task_run.started_at = started_at
    task_run.finished_at = finished_at
    db_session.commit()


def _build_run_with_one_change(client: TestClient, db_session, headers: dict):
    """Full setup: DETECT task/run + IssueDetectionRun, REMEDIATE task/run
    + RemediationRun, one Issue + one RemediationChange. Returns a dict of
    everything a test might need."""
    detect_task_id, detect_run_id, source_id = _create_task_and_run(client, headers, "detect")
    detect_task = db_session.get(Task, uuid.UUID(detect_task_id))
    detection_run = _insert_detection_run(
        db_session,
        organization_id=detect_task.organization_id,
        task_id=detect_task.id,
        task_run_id=uuid.UUID(detect_run_id),
        data_source_id=uuid.UUID(source_id),
    )
    issue = _insert_issue(
        db_session, organization_id=detect_task.organization_id, detection_run_id=detection_run.id
    )

    remediate_task_id, remediate_run_id, _ = _create_task_and_run(
        client, headers, "remediate", source_id=source_id, source_task_run_id=detect_run_id
    )
    remediate_task = db_session.get(Task, uuid.UUID(remediate_task_id))
    remediation_run = _insert_remediation_run(
        db_session,
        organization_id=remediate_task.organization_id,
        task_id=remediate_task.id,
        task_run_id=uuid.UUID(remediate_run_id),
        data_source_id=uuid.UUID(source_id),
        source_task_run_id=uuid.UUID(detect_run_id),
        issues_considered_count=1,
        total_changes_count=1,
        issues_skipped_count=0,
        changes_by_action={"trim_whitespace": 1},
    )
    change = _insert_change(
        db_session,
        organization_id=remediate_task.organization_id,
        remediation_run_id=remediation_run.id,
        source_issue_id=issue.id,
        row_number=2,
        column_name="name",
        action="trim_whitespace",
    )
    return {
        "task_id": remediate_task_id,
        "run_id": remediate_run_id,
        "source_id": source_id,
        "detect_run_id": detect_run_id,
        "detection_run": detection_run,
        "remediation_run": remediation_run,
        "issue": issue,
        "change": change,
        "org_id": remediate_task.organization_id,
    }


# --- GET .../remediation (summary) ------------------------------------------------


def test_get_remediation_summary_returns_200_with_expected_fields(
    client: TestClient, db_session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_run_with_one_change(client, db_session, headers)
    started = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    finished = started + timedelta(milliseconds=1500)
    _mark_completed(db_session, uuid.UUID(ctx["run_id"]), started_at=started, finished_at=finished)

    response = client.get(
        f"/tasks/{ctx['task_id']}/runs/{ctx['run_id']}/remediation", headers=headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(ctx["remediation_run"].id)
    assert body["task_run_id"] == ctx["run_id"]
    assert body["task_id"] == ctx["task_id"]
    assert body["data_source_id"] == ctx["source_id"]
    assert body["source_task_run_id"] == ctx["detect_run_id"]
    assert body["issues_considered_count"] == 1
    assert body["total_changes_count"] == 1
    assert body["issues_skipped_count"] == 0
    assert body["changes_by_action"] == {"trim_whitespace": 1}
    assert body["changes_by_column"] == {"name": 1}
    assert body["skipped_by_reason"] == {}
    assert body["remediation_engine_version"] == "1.0"
    assert body["dataset_sha256"] == "a" * 64
    assert body["processing_duration_ms"] == 1500
    assert set(body) == {
        "id", "organization_id", "task_run_id", "task_id", "data_source_id",
        "source_task_run_id", "issues_considered_count", "total_changes_count",
        "issues_skipped_count", "changes_by_action", "changes_by_column",
        "skipped_by_reason", "remediation_engine_version", "dataset_sha256",
        "processing_duration_ms", "created_at",
        # Module 16 Phase 3: decision breakdown added additively
        "decision_summary",
    }


def test_get_remediation_summary_duration_null_when_task_run_not_yet_finished(
    client: TestClient, db_session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_run_with_one_change(client, db_session, headers)
    # Deliberately never call _mark_completed -- TaskRun stays pending.

    response = client.get(
        f"/tasks/{ctx['task_id']}/runs/{ctx['run_id']}/remediation", headers=headers
    )

    assert response.status_code == 200, response.text
    assert response.json()["processing_duration_ms"] is None


def test_get_remediation_summary_skipped_by_reason_breakdown(
    client: TestClient, db_session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    detect_task_id, detect_run_id, source_id = _create_task_and_run(client, headers, "detect")
    detect_task = db_session.get(Task, uuid.UUID(detect_task_id))
    _insert_detection_run(
        db_session, organization_id=detect_task.organization_id, task_id=detect_task.id,
        task_run_id=uuid.UUID(detect_run_id), data_source_id=uuid.UUID(source_id),
    )
    remediate_task_id, remediate_run_id, _ = _create_task_and_run(
        client, headers, "remediate", source_id=source_id, source_task_run_id=detect_run_id
    )
    remediate_task = db_session.get(Task, uuid.UUID(remediate_task_id))
    _insert_remediation_run(
        db_session, organization_id=remediate_task.organization_id, task_id=remediate_task.id,
        task_run_id=uuid.UUID(remediate_run_id), data_source_id=uuid.UUID(source_id),
        source_task_run_id=uuid.UUID(detect_run_id),
        issues_considered_count=2, total_changes_count=0, issues_skipped_count=2,
        skipped_by_reason={"duplicate_removal_not_enabled": 2},
    )

    response = client.get(
        f"/tasks/{remediate_task_id}/runs/{remediate_run_id}/remediation", headers=headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["skipped_by_reason"] == {"duplicate_removal_not_enabled": 2}
    assert body["issues_skipped_count"] == 2
    assert body["total_changes_count"] == 0
    assert body["changes_by_action"] == {}
    assert body["changes_by_column"] == {}


def test_get_remediation_summary_404_when_no_run_yet(client: TestClient, db_session) -> None:
    headers = _register(client, uuid.uuid4().hex)
    task_id, run_id, _ = _create_task_and_run(client, headers, "detect")

    response = client.get(f"/tasks/{task_id}/runs/{run_id}/remediation", headers=headers)

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Remediation result not found"


def test_get_remediation_summary_404_for_nonexistent_task(client: TestClient, db_session) -> None:
    headers = _register(client, uuid.uuid4().hex)
    _, run_id, _ = _create_task_and_run(client, headers, "detect")

    response = client.get(f"/tasks/{uuid.uuid4()}/runs/{run_id}/remediation", headers=headers)

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task not found"


def test_get_remediation_summary_404_for_nonexistent_run(client: TestClient, db_session) -> None:
    headers = _register(client, uuid.uuid4().hex)
    task_id, _, _ = _create_task_and_run(client, headers, "detect")

    response = client.get(f"/tasks/{task_id}/runs/{uuid.uuid4()}/remediation", headers=headers)

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task run not found"


def test_get_remediation_summary_404_for_cross_org_task_and_run(
    client: TestClient, db_session
) -> None:
    owner_headers = _register(client, uuid.uuid4().hex)
    ctx = _build_run_with_one_change(client, db_session, owner_headers)

    other_headers = _register(client, uuid.uuid4().hex)

    response = client.get(
        f"/tasks/{ctx['task_id']}/runs/{ctx['run_id']}/remediation", headers=other_headers
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task not found"


# --- GET .../remediation/changes (paginated, filterable) ---------------------------


def _seed_changes(db_session, org_id, remediation_run_id, issue_id) -> None:
    _insert_change(
        db_session, organization_id=org_id, remediation_run_id=remediation_run_id,
        source_issue_id=issue_id, row_number=2, column_name="email", action="normalize_enum_value",
    )
    _insert_change(
        db_session, organization_id=org_id, remediation_run_id=remediation_run_id,
        source_issue_id=issue_id, row_number=3, column_name="name", action="trim_whitespace",
    )
    _insert_change(
        db_session, organization_id=org_id, remediation_run_id=remediation_run_id,
        source_issue_id=issue_id, row_number=4, column_name="email", action="normalize_enum_value",
    )
    _insert_change(
        db_session, organization_id=org_id, remediation_run_id=remediation_run_id,
        source_issue_id=issue_id, row_number=4, column_name=None, action="remove_duplicate_row",
        proposed_value=None,
    )


def test_list_remediation_changes_returns_all_with_pagination_envelope(
    client: TestClient, db_session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_run_with_one_change(client, db_session, headers)
    _seed_changes(db_session, ctx["org_id"], ctx["remediation_run"].id, ctx["issue"].id)

    response = client.get(
        f"/tasks/{ctx['task_id']}/runs/{ctx['run_id']}/remediation/changes", headers=headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    # The one change from _build_run_with_one_change plus the four seeded here.
    assert body["total"] == 5
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["items"]) == 5
    # Stable (row_number, id) order.
    assert [item["row_number"] for item in body["items"]] == [2, 2, 3, 4, 4]
    assert all(item["remediation_run_id"] == str(ctx["remediation_run"].id) for item in body["items"])
    assert all(item["source_issue_id"] == str(ctx["issue"].id) for item in body["items"])


def test_list_remediation_changes_pagination_limit_and_offset(
    client: TestClient, db_session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_run_with_one_change(client, db_session, headers)
    _seed_changes(db_session, ctx["org_id"], ctx["remediation_run"].id, ctx["issue"].id)

    response = client.get(
        f"/tasks/{ctx['task_id']}/runs/{ctx['run_id']}/remediation/changes",
        params={"limit": 2, "offset": 1},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 5
    assert body["limit"] == 2
    assert body["offset"] == 1
    assert len(body["items"]) == 2
    assert [item["row_number"] for item in body["items"]] == [2, 3]


def test_list_remediation_changes_pagination_limit_capped_at_100(
    client: TestClient, db_session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_run_with_one_change(client, db_session, headers)

    response = client.get(
        f"/tasks/{ctx['task_id']}/runs/{ctx['run_id']}/remediation/changes",
        params={"limit": 500},
        headers=headers,
    )

    assert response.status_code == 422, response.text


def test_list_remediation_changes_filters_by_action(client: TestClient, db_session) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_run_with_one_change(client, db_session, headers)
    _seed_changes(db_session, ctx["org_id"], ctx["remediation_run"].id, ctx["issue"].id)

    response = client.get(
        f"/tasks/{ctx['task_id']}/runs/{ctx['run_id']}/remediation/changes",
        params={"action": "normalize_enum_value"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert all(item["action"] == "normalize_enum_value" for item in body["items"])


def test_list_remediation_changes_filters_by_column_name(client: TestClient, db_session) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_run_with_one_change(client, db_session, headers)
    _seed_changes(db_session, ctx["org_id"], ctx["remediation_run"].id, ctx["issue"].id)

    response = client.get(
        f"/tasks/{ctx['task_id']}/runs/{ctx['run_id']}/remediation/changes",
        params={"column_name": "email"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert all(item["column_name"] == "email" for item in body["items"])


def test_list_remediation_changes_filters_by_row_number(client: TestClient, db_session) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_run_with_one_change(client, db_session, headers)
    _seed_changes(db_session, ctx["org_id"], ctx["remediation_run"].id, ctx["issue"].id)

    response = client.get(
        f"/tasks/{ctx['task_id']}/runs/{ctx['run_id']}/remediation/changes",
        params={"row_number": 4},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert all(item["row_number"] == 4 for item in body["items"])


def test_list_remediation_changes_combines_multiple_filters(
    client: TestClient, db_session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_run_with_one_change(client, db_session, headers)
    _seed_changes(db_session, ctx["org_id"], ctx["remediation_run"].id, ctx["issue"].id)

    response = client.get(
        f"/tasks/{ctx['task_id']}/runs/{ctx['run_id']}/remediation/changes",
        params={"row_number": 4, "action": "normalize_enum_value"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["column_name"] == "email"


def test_list_remediation_changes_rejects_invalid_action(client: TestClient, db_session) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_run_with_one_change(client, db_session, headers)

    response = client.get(
        f"/tasks/{ctx['task_id']}/runs/{ctx['run_id']}/remediation/changes",
        params={"action": "bogus"},
        headers=headers,
    )

    assert response.status_code == 422, response.text


def test_list_remediation_changes_whole_row_action_has_null_column_name(
    client: TestClient, db_session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_run_with_one_change(client, db_session, headers)
    _seed_changes(db_session, ctx["org_id"], ctx["remediation_run"].id, ctx["issue"].id)

    response = client.get(
        f"/tasks/{ctx['task_id']}/runs/{ctx['run_id']}/remediation/changes",
        params={"action": "remove_duplicate_row"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["column_name"] is None
    assert body["items"][0]["proposed_value"] is None


def test_list_remediation_changes_404_when_no_run_yet(client: TestClient, db_session) -> None:
    headers = _register(client, uuid.uuid4().hex)
    task_id, run_id, _ = _create_task_and_run(client, headers, "detect")

    response = client.get(
        f"/tasks/{task_id}/runs/{run_id}/remediation/changes", headers=headers
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Remediation result not found"


def test_list_remediation_changes_404_for_cross_org_task_and_run(
    client: TestClient, db_session
) -> None:
    owner_headers = _register(client, uuid.uuid4().hex)
    ctx = _build_run_with_one_change(client, db_session, owner_headers)
    _seed_changes(db_session, ctx["org_id"], ctx["remediation_run"].id, ctx["issue"].id)

    other_headers = _register(client, uuid.uuid4().hex)

    response = client.get(
        f"/tasks/{ctx['task_id']}/runs/{ctx['run_id']}/remediation/changes",
        headers=other_headers,
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task not found"


def test_list_remediation_changes_empty_run_returns_empty_page(
    client: TestClient, db_session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_run_with_one_change(client, db_session, headers)
    # Delete the one auto-created change so this run genuinely has zero.
    db_session.delete(ctx["change"])
    db_session.commit()

    response = client.get(
        f"/tasks/{ctx['task_id']}/runs/{ctx['run_id']}/remediation/changes", headers=headers
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 0
    assert body["items"] == []


def test_list_remediation_changes_large_dataset_pagination_is_correct(
    client: TestClient, db_session
) -> None:
    """500 changes across several pages -- proves pagination/ordering
    holds up beyond the small hand-seeded cases above."""
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_run_with_one_change(client, db_session, headers)
    db_session.delete(ctx["change"])
    db_session.commit()

    for row_number in range(2, 502):
        db_session.add(
            RemediationChange(
                organization_id=ctx["org_id"],
                remediation_run_id=ctx["remediation_run"].id,
                source_issue_id=ctx["issue"].id,
                row_number=row_number,
                column_name="name",
                action="trim_whitespace",
                original_value="  x",
                proposed_value="x",
                reason="test reason",
                confidence=1.0,
            )
        )
    db_session.commit()

    first_page = client.get(
        f"/tasks/{ctx['task_id']}/runs/{ctx['run_id']}/remediation/changes",
        params={"limit": 100, "offset": 0},
        headers=headers,
    ).json()
    last_page = client.get(
        f"/tasks/{ctx['task_id']}/runs/{ctx['run_id']}/remediation/changes",
        params={"limit": 100, "offset": 400},
        headers=headers,
    ).json()

    assert first_page["total"] == 500
    assert len(first_page["items"]) == 100
    assert [item["row_number"] for item in first_page["items"]] == list(range(2, 102))
    assert len(last_page["items"]) == 100
    assert [item["row_number"] for item in last_page["items"]] == list(range(402, 502))


def test_list_remediation_changes_stable_ordering_independent_of_insertion_order(
    client: TestClient, db_session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    ctx = _build_run_with_one_change(client, db_session, headers)
    db_session.delete(ctx["change"])
    db_session.commit()

    # Insert deliberately out of row_number order.
    for row_number in (9, 2, 5, 2, 7):
        db_session.add(
            RemediationChange(
                organization_id=ctx["org_id"],
                remediation_run_id=ctx["remediation_run"].id,
                source_issue_id=ctx["issue"].id,
                row_number=row_number,
                column_name="name",
                action="trim_whitespace",
                original_value="  x",
                proposed_value="x",
                reason="test reason",
                confidence=1.0,
            )
        )
    db_session.commit()

    response = client.get(
        f"/tasks/{ctx['task_id']}/runs/{ctx['run_id']}/remediation/changes", headers=headers
    )
    body = response.json()

    assert [item["row_number"] for item in body["items"]] == [2, 2, 5, 7, 9]
    # Calling again must return byte-identical ordering (no ORDER BY
    # ambiguity between the two row_number=2 rows -- id is the tiebreaker).
    response_again = client.get(
        f"/tasks/{ctx['task_id']}/runs/{ctx['run_id']}/remediation/changes", headers=headers
    )
    assert response_again.json()["items"] == body["items"]
