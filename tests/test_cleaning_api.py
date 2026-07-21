"""Module 6 integration tests for the cleaning API surface: the optional
TaskRunCreate body on POST /tasks/{id}/runs, the cleaning summary/changes
read endpoints, and the approve/reject/rollback state machine -- including
409 conflicts on invalid transitions and tenant isolation on every new
endpoint (same proof shape as Module 5's B1/B2 fixes)."""
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from app.core.config import get_settings
from app.models.data_source import DataSource
from app.models.task import Task
from app.models.task_run import TaskRun
from app.worker.handlers.base import ExecutionContext
from app.worker.handlers.cleaning import CleaningHandler
from app.worker.handlers.csv_profiling import CsvProfilingHandler

CSV_CONTENT = "id,name,amount\n1,  Ada  ,42.0\n2,Grace,3.140\n"


def _auth_headers(client: TestClient, suffix: str) -> dict:
    response = client.post(
        "/auth/register",
        json={
            "organization_name": f"Cleaning API Org {suffix}",
            "email": f"cleaning-api-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Cleaning API User",
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _create_sync_run(client: TestClient, headers: dict, source_id: str) -> tuple[str, str]:
    """Returns (sync_task_id, sync_run_id)."""
    task_response = client.post(
        "/tasks",
        json={"name": "Sync Customers", "task_type": "sync", "data_source_id": source_id},
        headers=headers,
    )
    assert task_response.status_code == 201, task_response.text
    run_response = client.post(f"/tasks/{task_response.json()['id']}/runs", headers=headers)
    assert run_response.status_code == 201, run_response.text
    return task_response.json()["id"], run_response.json()["id"]


def _create_data_source(client: TestClient, headers: dict, relative_path: str) -> str:
    response = client.post(
        "/data-sources",
        json={
            "name": "Uploaded Customers",
            "source_type": "csv_upload",
            "connection_metadata": {"file_path": relative_path},
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _build_cleaning_run(
    client: TestClient,
    db_session,
    csv_root: Path,
    headers: dict,
    *,
    relative_path: str = "customers.csv",
) -> tuple[str, str, str]:
    """Full pipeline through the real CleaningHandler, exactly mirroring
    test_cleaning_handler.py, so API tests exercise a genuine CleaningRun
    row rather than a hand-inserted fixture. Returns
    (transform_task_id, transform_run_id, organization_id)."""
    source_id = _create_data_source(client, headers, relative_path)
    sync_task_id, sync_run_id = _create_sync_run(client, headers, source_id)

    source = db_session.get(DataSource, uuid.UUID(source_id))
    org_dir = csv_root / str(source.organization_id)
    org_dir.mkdir(parents=True, exist_ok=True)
    (org_dir / relative_path).write_text(CSV_CONTENT, encoding="utf-8")

    sync_task = db_session.get(Task, uuid.UUID(sync_task_id))
    sync_run = db_session.get(TaskRun, uuid.UUID(sync_run_id))
    CsvProfilingHandler().execute(
        ExecutionContext(
            task_run=sync_run,
            task=sync_task,
            data_source=source,
            idempotency_key=str(sync_run.idempotency_key),
            credential_provider=None,
        )
    )

    transform_task_response = client.post(
        "/tasks",
        json={"name": "Clean Customers", "task_type": "transform", "data_source_id": source_id},
        headers=headers,
    )
    assert transform_task_response.status_code == 201, transform_task_response.text
    transform_task_id = transform_task_response.json()["id"]

    transform_run_response = client.post(
        f"/tasks/{transform_task_id}/runs",
        json={"source_task_run_id": sync_run_id},
        headers=headers,
    )
    assert transform_run_response.status_code == 201, transform_run_response.text
    transform_run_id = transform_run_response.json()["id"]

    transform_task = db_session.get(Task, uuid.UUID(transform_task_id))
    transform_run = db_session.get(TaskRun, uuid.UUID(transform_run_id))
    CleaningHandler().execute(
        ExecutionContext(
            task_run=transform_run,
            task=transform_task,
            data_source=source,
            idempotency_key=str(transform_run.idempotency_key),
            credential_provider=None,
        )
    )
    return transform_task_id, transform_run_id, str(source.organization_id)


# --- POST /tasks/{id}/runs: TaskRunCreate / source_task_run_id validation ---


def test_create_run_for_transform_task_without_source_task_run_id_is_400(
    client: TestClient,
) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    task_response = client.post(
        "/tasks", json={"name": "Clean", "task_type": "transform"}, headers=headers
    )
    response = client.post(f"/tasks/{task_response.json()['id']}/runs", headers=headers)
    assert response.status_code == 400, response.text
    assert "source_task_run_id is required" in response.json()["detail"]


def test_create_run_for_transform_task_with_nonexistent_source_run_is_404(
    client: TestClient,
) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    task_response = client.post(
        "/tasks", json={"name": "Clean", "task_type": "transform"}, headers=headers
    )
    response = client.post(
        f"/tasks/{task_response.json()['id']}/runs",
        json={"source_task_run_id": str(uuid.uuid4())},
        headers=headers,
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Source task run not found"


def test_create_run_for_transform_task_with_cross_org_source_run_is_404(
    client: TestClient,
) -> None:
    owner_headers = _auth_headers(client, uuid.uuid4().hex)
    source_id = _create_data_source(client, owner_headers, "customers.csv")
    _, sync_run_id = _create_sync_run(client, owner_headers, source_id)

    other_headers = _auth_headers(client, uuid.uuid4().hex)
    task_response = client.post(
        "/tasks", json={"name": "Clean", "task_type": "transform"}, headers=other_headers
    )
    response = client.post(
        f"/tasks/{task_response.json()['id']}/runs",
        json={"source_task_run_id": sync_run_id},
        headers=other_headers,
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Source task run not found"


def test_create_run_for_non_transform_task_with_source_task_run_id_is_400(
    client: TestClient,
) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    source_id = _create_data_source(client, headers, "customers.csv")
    _, sync_run_id = _create_sync_run(client, headers, source_id)

    other_sync_task_response = client.post(
        "/tasks",
        json={"name": "Another Sync", "task_type": "sync", "data_source_id": source_id},
        headers=headers,
    )
    response = client.post(
        f"/tasks/{other_sync_task_response.json()['id']}/runs",
        json={"source_task_run_id": sync_run_id},
        headers=headers,
    )
    assert response.status_code == 400, response.text
    assert "only valid for TRANSFORM" in response.json()["detail"]


def test_create_run_without_body_is_unaffected_for_sync_tasks(client: TestClient) -> None:
    """Backward compatibility: existing callers sending no body at all
    still work exactly as before Module 6."""
    headers = _auth_headers(client, uuid.uuid4().hex)
    task_response = client.post(
        "/tasks", json={"name": "Sync", "task_type": "sync"}, headers=headers
    )
    response = client.post(f"/tasks/{task_response.json()['id']}/runs", headers=headers)
    assert response.status_code == 201, response.text
    assert response.json()["source_task_run_id"] is None


def test_create_run_for_transform_task_with_valid_source_run_succeeds(
    client: TestClient,
) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    source_id = _create_data_source(client, headers, "customers.csv")
    _, sync_run_id = _create_sync_run(client, headers, source_id)

    task_response = client.post(
        "/tasks",
        json={"name": "Clean", "task_type": "transform", "data_source_id": source_id},
        headers=headers,
    )
    response = client.post(
        f"/tasks/{task_response.json()['id']}/runs",
        json={"source_task_run_id": sync_run_id},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["source_task_run_id"] == sync_run_id


# --- GET .../cleaning ----------------------------------------------------------


def test_get_task_run_cleaning_returns_200_after_successful_execution(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CSV_INPUT_ROOT", str(tmp_path / "csv_in"))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(tmp_path / "csv_out"))
    get_settings.cache_clear()
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        task_id, run_id, _ = _build_cleaning_run(
            client, db_session, tmp_path / "csv_in", headers
        )

        response = client.get(f"/tasks/{task_id}/runs/{run_id}/cleaning", headers=headers)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["task_run_id"] == run_id
        assert body["status"] == "pending_review"
        assert body["cleaning_engine_version"] == "1.0"
        assert body["total_changes_count"] > 0
    finally:
        get_settings.cache_clear()


def test_get_task_run_cleaning_404_when_no_result_yet(client: TestClient) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    source_id = _create_data_source(client, headers, "customers.csv")
    task_response = client.post(
        "/tasks",
        json={"name": "Clean", "task_type": "transform", "data_source_id": source_id},
        headers=headers,
    )
    _, sync_run_id = _create_sync_run(client, headers, source_id)
    run_response = client.post(
        f"/tasks/{task_response.json()['id']}/runs",
        json={"source_task_run_id": sync_run_id},
        headers=headers,
    )

    response = client.get(
        f"/tasks/{task_response.json()['id']}/runs/{run_response.json()['id']}/cleaning",
        headers=headers,
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Cleaning result not found"


def test_get_task_run_cleaning_404_for_cross_org_access(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CSV_INPUT_ROOT", str(tmp_path / "csv_in"))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(tmp_path / "csv_out"))
    get_settings.cache_clear()
    try:
        owner_headers = _auth_headers(client, uuid.uuid4().hex)
        task_id, run_id, _ = _build_cleaning_run(
            client, db_session, tmp_path / "csv_in", owner_headers
        )

        other_headers = _auth_headers(client, uuid.uuid4().hex)
        response = client.get(
            f"/tasks/{task_id}/runs/{run_id}/cleaning", headers=other_headers
        )
        assert response.status_code == 404, response.text
        assert response.json()["detail"] == "Task not found"
    finally:
        get_settings.cache_clear()


# --- GET .../cleaning/changes ---------------------------------------------------


def test_list_task_run_cleaning_changes_returns_paginated_changes(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CSV_INPUT_ROOT", str(tmp_path / "csv_in"))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(tmp_path / "csv_out"))
    get_settings.cache_clear()
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        task_id, run_id, _ = _build_cleaning_run(
            client, db_session, tmp_path / "csv_in", headers
        )

        response = client.get(
            f"/tasks/{task_id}/runs/{run_id}/cleaning/changes", headers=headers
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] > 0
        assert len(body["items"]) == body["total"]
        for item in body["items"]:
            assert item["rule_name"]
            assert item["reason"]
    finally:
        get_settings.cache_clear()


# --- Approval state machine ------------------------------------------------------


def test_approve_transitions_pending_review_to_approved(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CSV_INPUT_ROOT", str(tmp_path / "csv_in"))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(tmp_path / "csv_out"))
    get_settings.cache_clear()
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        task_id, run_id, _ = _build_cleaning_run(
            client, db_session, tmp_path / "csv_in", headers
        )

        response = client.post(
            f"/tasks/{task_id}/runs/{run_id}/cleaning/approve", headers=headers
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "approved"
        assert body["approved_by"] is not None
        assert body["approved_at"] is not None
    finally:
        get_settings.cache_clear()


def test_approve_on_already_approved_run_is_409(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CSV_INPUT_ROOT", str(tmp_path / "csv_in"))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(tmp_path / "csv_out"))
    get_settings.cache_clear()
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        task_id, run_id, _ = _build_cleaning_run(
            client, db_session, tmp_path / "csv_in", headers
        )
        first = client.post(f"/tasks/{task_id}/runs/{run_id}/cleaning/approve", headers=headers)
        assert first.status_code == 200, first.text

        second = client.post(f"/tasks/{task_id}/runs/{run_id}/cleaning/approve", headers=headers)
        assert second.status_code == 409, second.text
        assert "approved" in second.json()["detail"]
    finally:
        get_settings.cache_clear()


def test_reject_transitions_pending_review_to_rejected(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CSV_INPUT_ROOT", str(tmp_path / "csv_in"))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(tmp_path / "csv_out"))
    get_settings.cache_clear()
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        task_id, run_id, _ = _build_cleaning_run(
            client, db_session, tmp_path / "csv_in", headers
        )

        response = client.post(
            f"/tasks/{task_id}/runs/{run_id}/cleaning/reject", headers=headers
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "rejected"
        assert body["rejected_by"] is not None
        assert body["rejected_at"] is not None
    finally:
        get_settings.cache_clear()


def test_reject_on_approved_run_is_409(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CSV_INPUT_ROOT", str(tmp_path / "csv_in"))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(tmp_path / "csv_out"))
    get_settings.cache_clear()
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        task_id, run_id, _ = _build_cleaning_run(
            client, db_session, tmp_path / "csv_in", headers
        )
        approve = client.post(f"/tasks/{task_id}/runs/{run_id}/cleaning/approve", headers=headers)
        assert approve.status_code == 200, approve.text

        response = client.post(f"/tasks/{task_id}/runs/{run_id}/cleaning/reject", headers=headers)
        assert response.status_code == 409, response.text
        assert "approved" in response.json()["detail"]
    finally:
        get_settings.cache_clear()


def test_rollback_transitions_approved_to_rolled_back(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CSV_INPUT_ROOT", str(tmp_path / "csv_in"))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(tmp_path / "csv_out"))
    get_settings.cache_clear()
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        task_id, run_id, _ = _build_cleaning_run(
            client, db_session, tmp_path / "csv_in", headers
        )
        approve = client.post(f"/tasks/{task_id}/runs/{run_id}/cleaning/approve", headers=headers)
        assert approve.status_code == 200, approve.text

        response = client.post(
            f"/tasks/{task_id}/runs/{run_id}/cleaning/rollback", headers=headers
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "rolled_back"
        assert body["rolled_back_by"] is not None
        assert body["rolled_back_at"] is not None
    finally:
        get_settings.cache_clear()


def test_rollback_on_pending_review_run_is_409(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Rollback requires an APPROVED run -- pending_review cannot be
    rolled back directly, only approved -> rolled_back is a valid edge."""
    monkeypatch.setenv("CSV_INPUT_ROOT", str(tmp_path / "csv_in"))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(tmp_path / "csv_out"))
    get_settings.cache_clear()
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        task_id, run_id, _ = _build_cleaning_run(
            client, db_session, tmp_path / "csv_in", headers
        )

        response = client.post(
            f"/tasks/{task_id}/runs/{run_id}/cleaning/rollback", headers=headers
        )
        assert response.status_code == 409, response.text
        assert "pending_review" in response.json()["detail"]
    finally:
        get_settings.cache_clear()


def test_rollback_on_rejected_run_is_409(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CSV_INPUT_ROOT", str(tmp_path / "csv_in"))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(tmp_path / "csv_out"))
    get_settings.cache_clear()
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        task_id, run_id, _ = _build_cleaning_run(
            client, db_session, tmp_path / "csv_in", headers
        )
        reject = client.post(f"/tasks/{task_id}/runs/{run_id}/cleaning/reject", headers=headers)
        assert reject.status_code == 200, reject.text

        response = client.post(
            f"/tasks/{task_id}/runs/{run_id}/cleaning/rollback", headers=headers
        )
        assert response.status_code == 409, response.text
        assert "rejected" in response.json()["detail"]
    finally:
        get_settings.cache_clear()


def test_rollback_does_not_delete_cleaning_changes(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Acceptance Criteria: rollback tested end-to-end and confirmed
    non-destructive -- the CleaningChange audit trail must remain fully
    readable after a rollback."""
    monkeypatch.setenv("CSV_INPUT_ROOT", str(tmp_path / "csv_in"))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(tmp_path / "csv_out"))
    get_settings.cache_clear()
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        task_id, run_id, _ = _build_cleaning_run(
            client, db_session, tmp_path / "csv_in", headers
        )
        before = client.get(f"/tasks/{task_id}/runs/{run_id}/cleaning/changes", headers=headers)
        assert before.status_code == 200
        before_total = before.json()["total"]
        assert before_total > 0

        client.post(f"/tasks/{task_id}/runs/{run_id}/cleaning/approve", headers=headers)
        rollback = client.post(
            f"/tasks/{task_id}/runs/{run_id}/cleaning/rollback", headers=headers
        )
        assert rollback.status_code == 200, rollback.text

        after = client.get(f"/tasks/{task_id}/runs/{run_id}/cleaning/changes", headers=headers)
        assert after.status_code == 200
        assert after.json()["total"] == before_total
    finally:
        get_settings.cache_clear()


def test_approve_reject_rollback_404_for_cross_org_access(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CSV_INPUT_ROOT", str(tmp_path / "csv_in"))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(tmp_path / "csv_out"))
    get_settings.cache_clear()
    try:
        owner_headers = _auth_headers(client, uuid.uuid4().hex)
        task_id, run_id, _ = _build_cleaning_run(
            client, db_session, tmp_path / "csv_in", owner_headers
        )

        other_headers = _auth_headers(client, uuid.uuid4().hex)
        for action in ("approve", "reject", "rollback"):
            response = client.post(
                f"/tasks/{task_id}/runs/{run_id}/cleaning/{action}", headers=other_headers
            )
            assert response.status_code == 404, response.text
            assert response.json()["detail"] == "Task not found"
    finally:
        get_settings.cache_clear()
