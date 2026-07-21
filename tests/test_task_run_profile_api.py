"""Module 5 B2: coverage for GET /tasks/{task_id}/runs/{run_id}/profile.

Every other endpoint in api/tasks.py has explicit tests for its 404/cross-
org/not-found behavior; this endpoint shipped without any. These tests
close that gap directly against the API (not the handler, which is already
covered by test_csv_profiling_handler.py) -- auth, tenant scoping, and
response serialization only.
"""
import uuid

from fastapi.testclient import TestClient

from app.models.data_profile import DataProfile
from app.models.data_source import DataSource
from app.models.task import Task
from app.models.task_run import TaskRun


def _register(client: TestClient, suffix: str) -> dict:
    response = client.post(
        "/auth/register",
        json={
            "organization_name": f"Profile API Org {suffix}",
            "email": f"profile-api-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Profile API User",
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
            "name": "Profile Customers",
            "task_type": "sync",
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


def _insert_profile(
    db_session,
    *,
    organization_id: uuid.UUID,
    task_id: uuid.UUID,
    task_run_id: uuid.UUID,
    data_source_id: uuid.UUID,
) -> DataProfile:
    profile = DataProfile(
        organization_id=organization_id,
        task_run_id=task_run_id,
        task_id=task_id,
        data_source_id=data_source_id,
        source_filename="customers.csv",
        source_size_bytes=42,
        source_sha256="a" * 64,
        detected_encoding="utf-8",
        delimiter=",",
        row_count=3,
        column_count=2,
        duplicate_row_count=0,
        missing_value_total=0,
        column_profiles=[],
        structural_issues=[],
        limits_applied={},
    )
    db_session.add(profile)
    db_session.commit()
    db_session.refresh(profile)
    return profile


def test_get_task_run_profile_returns_200_after_successful_execution(
    client: TestClient, db_session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    task_id, run_id, source_id = _create_task_and_run(client, headers)
    task = db_session.get(Task, uuid.UUID(task_id))
    profile = _insert_profile(
        db_session,
        organization_id=task.organization_id,
        task_id=task.id,
        task_run_id=uuid.UUID(run_id),
        data_source_id=uuid.UUID(source_id),
    )

    response = client.get(f"/tasks/{task_id}/runs/{run_id}/profile", headers=headers)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == str(profile.id)
    assert body["task_run_id"] == run_id
    assert body["task_id"] == task_id
    assert body["row_count"] == 3
    assert body["column_count"] == 2


def test_get_task_run_profile_404_when_no_profile_yet(client: TestClient, db_session) -> None:
    headers = _register(client, uuid.uuid4().hex)
    task_id, run_id, _ = _create_task_and_run(client, headers)

    response = client.get(f"/tasks/{task_id}/runs/{run_id}/profile", headers=headers)

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Data profile not found"


def test_get_task_run_profile_404_for_cross_org_task_and_run(
    client: TestClient, db_session
) -> None:
    owner_headers = _register(client, uuid.uuid4().hex)
    task_id, run_id, source_id = _create_task_and_run(client, owner_headers)
    task = db_session.get(Task, uuid.UUID(task_id))
    _insert_profile(
        db_session,
        organization_id=task.organization_id,
        task_id=task.id,
        task_run_id=uuid.UUID(run_id),
        data_source_id=uuid.UUID(source_id),
    )

    other_headers = _register(client, uuid.uuid4().hex)

    response = client.get(f"/tasks/{task_id}/runs/{run_id}/profile", headers=other_headers)

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task not found"


def test_get_task_run_profile_404_for_nonexistent_task(client: TestClient, db_session) -> None:
    headers = _register(client, uuid.uuid4().hex)
    _, run_id, _ = _create_task_and_run(client, headers)

    response = client.get(
        f"/tasks/{uuid.uuid4()}/runs/{run_id}/profile", headers=headers
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task not found"


def test_get_task_run_profile_404_for_nonexistent_run(client: TestClient, db_session) -> None:
    headers = _register(client, uuid.uuid4().hex)
    task_id, _, _ = _create_task_and_run(client, headers)

    response = client.get(
        f"/tasks/{task_id}/runs/{uuid.uuid4()}/profile", headers=headers
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task run not found"


def test_get_task_run_profile_404_when_run_accessed_through_wrong_task(
    client: TestClient, db_session
) -> None:
    headers = _register(client, uuid.uuid4().hex)
    task_id, run_id, source_id = _create_task_and_run(client, headers)
    task = db_session.get(Task, uuid.UUID(task_id))
    _insert_profile(
        db_session,
        organization_id=task.organization_id,
        task_id=task.id,
        task_run_id=uuid.UUID(run_id),
        data_source_id=uuid.UUID(source_id),
    )

    # A second, unrelated task in the SAME org -- the run belongs to the
    # first task, not this one.
    other_task_response = client.post(
        "/tasks",
        json={"name": "Unrelated Task", "task_type": "sync"},
        headers=headers,
    )
    assert other_task_response.status_code == 201, other_task_response.text
    other_task_id = other_task_response.json()["id"]

    response = client.get(
        f"/tasks/{other_task_id}/runs/{run_id}/profile", headers=headers
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Task run not found"
