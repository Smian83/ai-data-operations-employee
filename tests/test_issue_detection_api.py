"""Module 14 Phase 3: coverage for
GET /tasks/{task_id}/runs/{run_id}/detection (summary) and
GET /tasks/{task_id}/runs/{run_id}/detection/issues (paginated, filterable
detail). Mirrors test_task_run_profile_api.py's auth/tenant-scoping/404
conventions, plus pagination/filter coverage in the style of the
matching-decisions list endpoint tests."""
import uuid

from fastapi.testclient import TestClient

from app.models.issue import Issue
from app.models.issue_detection_run import IssueDetectionRun
from app.models.task import Task


def _register(client: TestClient, suffix: str) -> dict:
    response = client.post(
        "/auth/register",
        json={
            "organization_name": f"Detect API Org {suffix}",
            "email": f"detect-api-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Detect API User",
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_task_and_run(client: TestClient, headers: dict) -> tuple[str, str, str]:
    """Returns (task_id, run_id, data_source_id)."""
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
    task_response = client.post(
        "/tasks",
        json={
            "name": "Detect Issues",
            "task_type": "detect",
            "data_source_id": source_response.json()["id"],
        },
        headers=headers,
    )
    assert task_response.status_code == 201, task_response.text
    run_response = client.post(
        f"/tasks/{task_response.json()['id']}/runs",
        headers=headers,
    )
    assert run_response.status_code == 201, run_response.text
    return task_response.json()["id"], run_response.json()["id"], source_response.json()["id"]


def _insert_detection_run(
    db_session,
    *,
    organization_id: uuid.UUID,
    task_id: uuid.UUID,
    task_run_id: uuid.UUID,
    data_source_id: uuid.UUID,
    total_issues_found: int = 0,
    persisted_issue_count: int = 0,
    issues_by_severity: dict | None = None,
    issues_by_type: dict | None = None,
) -> IssueDetectionRun:
    run = IssueDetectionRun(
        organization_id=organization_id,
        task_run_id=task_run_id,
        task_id=task_id,
        data_source_id=data_source_id,
        rows_scanned=10,
        columns_scanned=3,
        total_issues_found=total_issues_found,
        persisted_issue_count=persisted_issue_count,
        issues_by_severity=issues_by_severity
        or {"INFO": 0, "LOW": 0, "MEDIUM": 0, "HIGH": 0, "CRITICAL": 0},
        issues_by_type=issues_by_type or {},
        limits_applied={"max_persisted_issues": 10_000},
        detection_engine_version="1.0",
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


def _insert_issue(
    db_session,
    *,
    organization_id: uuid.UUID,
    detection_run_id: uuid.UUID,
    row_number: int,
    column_name: str | None,
    issue_type: str,
    severity: str,
    confidence: float = 1.0,
) -> Issue:
    issue = Issue(
        organization_id=organization_id,
        detection_run_id=detection_run_id,
        row_number=row_number,
        column_name=column_name,
        issue_type=issue_type,
        severity=severity,
        original_value=None,
        suggested_fix=None,
        confidence=confidence,
    )
    db_session.add(issue)
    db_session.commit()
    db_session.refresh(issue)
    return issue


# --- GET .../detection (summary) --------------------------------------------------


def test_get_detection_summary_returns_200_with_expected_fields(
    client: TestClient, db_session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    task_id, run_id, source_id = _create_task_and_run(client, headers)
    task = db_session.get(Task, uuid.UUID(task_id))
    detection_run = _insert_detection_run(
        db_session,
        organization_id=task.organization_id,
        task_id=task.id,
        task_run_id=uuid.UUID(run_id),
        data_source_id=uuid.UUID(source_id),
        total_issues_found=5,
        persisted_issue_count=5,
        issues_by_severity={"INFO": 2, "LOW": 1, "MEDIUM": 1, "HIGH": 1, "CRITICAL": 0},
        issues_by_type={"leading_whitespace": 2, "invalid_email": 3},
    )

    response = client.get(f"/tasks/{task_id}/runs/{run_id}/detection", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(detection_run.id)
    assert body["task_run_id"] == run_id
    assert body["task_id"] == task_id
    assert body["data_source_id"] == source_id
    assert body["rows_scanned"] == 10
    assert body["columns_scanned"] == 3
    assert body["total_issues_found"] == 5
    assert body["persisted_issue_count"] == 5
    assert body["issues_by_severity"] == {
        "INFO": 2, "LOW": 1, "MEDIUM": 1, "HIGH": 1, "CRITICAL": 0
    }
    assert body["issues_by_type"] == {"leading_whitespace": 2, "invalid_email": 3}
    assert body["detection_engine_version"] == "1.0"
    # Never exposes a filesystem path or anything else off the raw model
    # beyond this explicit, documented field set.
    assert set(body) == {
        "id", "organization_id", "task_run_id", "task_id", "data_source_id",
        "rows_scanned", "columns_scanned", "total_issues_found",
        "persisted_issue_count", "issues_by_severity", "issues_by_type",
        "limits_applied", "detection_engine_version", "detected_at",
    }


def test_get_detection_summary_404_when_no_run_yet(client: TestClient, db_session) -> None:
    headers = _register(client, uuid.uuid4().hex)
    task_id, run_id, _ = _create_task_and_run(client, headers)

    response = client.get(f"/tasks/{task_id}/runs/{run_id}/detection", headers=headers)

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Issue detection result not found"


def test_get_detection_summary_404_for_cross_org_task_and_run(
    client: TestClient, db_session
) -> None:
    owner_headers = _register(client, uuid.uuid4().hex)
    task_id, run_id, source_id = _create_task_and_run(client, owner_headers)
    task = db_session.get(Task, uuid.UUID(task_id))
    _insert_detection_run(
        db_session,
        organization_id=task.organization_id,
        task_id=task.id,
        task_run_id=uuid.UUID(run_id),
        data_source_id=uuid.UUID(source_id),
    )

    other_headers = _register(client, uuid.uuid4().hex)

    response = client.get(f"/tasks/{task_id}/runs/{run_id}/detection", headers=other_headers)

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task not found"


def test_get_detection_summary_404_for_nonexistent_task(client: TestClient, db_session) -> None:
    headers = _register(client, uuid.uuid4().hex)
    _, run_id, _ = _create_task_and_run(client, headers)

    response = client.get(
        f"/tasks/{uuid.uuid4()}/runs/{run_id}/detection", headers=headers
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task not found"


def test_get_detection_summary_404_for_nonexistent_run(client: TestClient, db_session) -> None:
    headers = _register(client, uuid.uuid4().hex)
    task_id, _, _ = _create_task_and_run(client, headers)

    response = client.get(
        f"/tasks/{task_id}/runs/{uuid.uuid4()}/detection", headers=headers
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task run not found"


# --- GET .../detection/issues (paginated, filterable detail) -----------------------


def _seed_issues(db_session, organization_id, detection_run_id) -> None:
    _insert_issue(
        db_session, organization_id=organization_id, detection_run_id=detection_run_id,
        row_number=2, column_name="email", issue_type="invalid_email", severity="MEDIUM",
    )
    _insert_issue(
        db_session, organization_id=organization_id, detection_run_id=detection_run_id,
        row_number=3, column_name="name", issue_type="leading_whitespace", severity="INFO",
    )
    _insert_issue(
        db_session, organization_id=organization_id, detection_run_id=detection_run_id,
        row_number=4, column_name="email", issue_type="required_field_violation", severity="HIGH",
    )
    _insert_issue(
        db_session, organization_id=organization_id, detection_run_id=detection_run_id,
        row_number=4, column_name=None, issue_type="duplicate_row", severity="MEDIUM",
    )


def test_list_detection_issues_returns_all_with_dataset_id_and_pagination_envelope(
    client: TestClient, db_session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    task_id, run_id, source_id = _create_task_and_run(client, headers)
    task = db_session.get(Task, uuid.UUID(task_id))
    detection_run = _insert_detection_run(
        db_session,
        organization_id=task.organization_id,
        task_id=task.id,
        task_run_id=uuid.UUID(run_id),
        data_source_id=uuid.UUID(source_id),
        total_issues_found=4,
        persisted_issue_count=4,
    )
    _seed_issues(db_session, task.organization_id, detection_run.id)

    response = client.get(f"/tasks/{task_id}/runs/{run_id}/detection/issues", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 4
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert len(body["items"]) == 4
    # Deterministic row_number order.
    assert [item["row_number"] for item in body["items"]] == [2, 3, 4, 4]
    # dataset_id is populated from the parent run's data_source_id even
    # though it is not a column on Issue.
    assert all(item["dataset_id"] == source_id for item in body["items"])
    assert all(item["detection_run_id"] == str(detection_run.id) for item in body["items"])


def test_list_detection_issues_pagination_limit_and_offset(
    client: TestClient, db_session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    task_id, run_id, source_id = _create_task_and_run(client, headers)
    task = db_session.get(Task, uuid.UUID(task_id))
    detection_run = _insert_detection_run(
        db_session,
        organization_id=task.organization_id,
        task_id=task.id,
        task_run_id=uuid.UUID(run_id),
        data_source_id=uuid.UUID(source_id),
        total_issues_found=4,
        persisted_issue_count=4,
    )
    _seed_issues(db_session, task.organization_id, detection_run.id)

    response = client.get(
        f"/tasks/{task_id}/runs/{run_id}/detection/issues",
        params={"limit": 2, "offset": 1},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 4
    assert body["limit"] == 2
    assert body["offset"] == 1
    assert len(body["items"]) == 2
    assert [item["row_number"] for item in body["items"]] == [3, 4]


def test_list_detection_issues_pagination_limit_capped_at_100(
    client: TestClient, db_session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    task_id, run_id, _ = _create_task_and_run(client, headers)

    response = client.get(
        f"/tasks/{task_id}/runs/{run_id}/detection/issues",
        params={"limit": 500},
        headers=headers,
    )

    assert response.status_code == 422, response.text


def test_list_detection_issues_filters_by_severity(client: TestClient, db_session) -> None:
    headers = _register(client, uuid.uuid4().hex)
    task_id, run_id, source_id = _create_task_and_run(client, headers)
    task = db_session.get(Task, uuid.UUID(task_id))
    detection_run = _insert_detection_run(
        db_session,
        organization_id=task.organization_id,
        task_id=task.id,
        task_run_id=uuid.UUID(run_id),
        data_source_id=uuid.UUID(source_id),
    )
    _seed_issues(db_session, task.organization_id, detection_run.id)

    response = client.get(
        f"/tasks/{task_id}/runs/{run_id}/detection/issues",
        params={"severity": "MEDIUM"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert all(item["severity"] == "MEDIUM" for item in body["items"])


def test_list_detection_issues_filters_by_issue_type(client: TestClient, db_session) -> None:
    headers = _register(client, uuid.uuid4().hex)
    task_id, run_id, source_id = _create_task_and_run(client, headers)
    task = db_session.get(Task, uuid.UUID(task_id))
    detection_run = _insert_detection_run(
        db_session,
        organization_id=task.organization_id,
        task_id=task.id,
        task_run_id=uuid.UUID(run_id),
        data_source_id=uuid.UUID(source_id),
    )
    _seed_issues(db_session, task.organization_id, detection_run.id)

    response = client.get(
        f"/tasks/{task_id}/runs/{run_id}/detection/issues",
        params={"issue_type": "invalid_email"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["issue_type"] == "invalid_email"


def test_list_detection_issues_filters_by_column_name(client: TestClient, db_session) -> None:
    headers = _register(client, uuid.uuid4().hex)
    task_id, run_id, source_id = _create_task_and_run(client, headers)
    task = db_session.get(Task, uuid.UUID(task_id))
    detection_run = _insert_detection_run(
        db_session,
        organization_id=task.organization_id,
        task_id=task.id,
        task_run_id=uuid.UUID(run_id),
        data_source_id=uuid.UUID(source_id),
    )
    _seed_issues(db_session, task.organization_id, detection_run.id)

    response = client.get(
        f"/tasks/{task_id}/runs/{run_id}/detection/issues",
        params={"column_name": "email"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert all(item["column_name"] == "email" for item in body["items"])


def test_list_detection_issues_filters_by_row_number(client: TestClient, db_session) -> None:
    headers = _register(client, uuid.uuid4().hex)
    task_id, run_id, source_id = _create_task_and_run(client, headers)
    task = db_session.get(Task, uuid.UUID(task_id))
    detection_run = _insert_detection_run(
        db_session,
        organization_id=task.organization_id,
        task_id=task.id,
        task_run_id=uuid.UUID(run_id),
        data_source_id=uuid.UUID(source_id),
    )
    _seed_issues(db_session, task.organization_id, detection_run.id)

    response = client.get(
        f"/tasks/{task_id}/runs/{run_id}/detection/issues",
        params={"row_number": 4},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 2
    assert all(item["row_number"] == 4 for item in body["items"])


def test_list_detection_issues_combines_multiple_filters(client: TestClient, db_session) -> None:
    headers = _register(client, uuid.uuid4().hex)
    task_id, run_id, source_id = _create_task_and_run(client, headers)
    task = db_session.get(Task, uuid.UUID(task_id))
    detection_run = _insert_detection_run(
        db_session,
        organization_id=task.organization_id,
        task_id=task.id,
        task_run_id=uuid.UUID(run_id),
        data_source_id=uuid.UUID(source_id),
    )
    _seed_issues(db_session, task.organization_id, detection_run.id)

    response = client.get(
        f"/tasks/{task_id}/runs/{run_id}/detection/issues",
        params={"row_number": 4, "severity": "MEDIUM"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["issue_type"] == "duplicate_row"


def test_list_detection_issues_rejects_invalid_severity(client: TestClient, db_session) -> None:
    headers = _register(client, uuid.uuid4().hex)
    task_id, run_id, source_id = _create_task_and_run(client, headers)
    task = db_session.get(Task, uuid.UUID(task_id))
    _insert_detection_run(
        db_session,
        organization_id=task.organization_id,
        task_id=task.id,
        task_run_id=uuid.UUID(run_id),
        data_source_id=uuid.UUID(source_id),
    )

    response = client.get(
        f"/tasks/{task_id}/runs/{run_id}/detection/issues",
        params={"severity": "bogus"},
        headers=headers,
    )

    assert response.status_code == 422, response.text


def test_list_detection_issues_rejects_invalid_issue_type(client: TestClient, db_session) -> None:
    headers = _register(client, uuid.uuid4().hex)
    task_id, run_id, source_id = _create_task_and_run(client, headers)
    task = db_session.get(Task, uuid.UUID(task_id))
    _insert_detection_run(
        db_session,
        organization_id=task.organization_id,
        task_id=task.id,
        task_run_id=uuid.UUID(run_id),
        data_source_id=uuid.UUID(source_id),
    )

    response = client.get(
        f"/tasks/{task_id}/runs/{run_id}/detection/issues",
        params={"issue_type": "bogus"},
        headers=headers,
    )

    assert response.status_code == 422, response.text


def test_list_detection_issues_404_when_no_run_yet(client: TestClient, db_session) -> None:
    headers = _register(client, uuid.uuid4().hex)
    task_id, run_id, _ = _create_task_and_run(client, headers)

    response = client.get(
        f"/tasks/{task_id}/runs/{run_id}/detection/issues", headers=headers
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Issue detection result not found"


def test_list_detection_issues_404_for_cross_org_task_and_run(
    client: TestClient, db_session
) -> None:
    owner_headers = _register(client, uuid.uuid4().hex)
    task_id, run_id, source_id = _create_task_and_run(client, owner_headers)
    task = db_session.get(Task, uuid.UUID(task_id))
    detection_run = _insert_detection_run(
        db_session,
        organization_id=task.organization_id,
        task_id=task.id,
        task_run_id=uuid.UUID(run_id),
        data_source_id=uuid.UUID(source_id),
    )
    _seed_issues(db_session, task.organization_id, detection_run.id)

    other_headers = _register(client, uuid.uuid4().hex)

    response = client.get(
        f"/tasks/{task_id}/runs/{run_id}/detection/issues", headers=other_headers
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task not found"
