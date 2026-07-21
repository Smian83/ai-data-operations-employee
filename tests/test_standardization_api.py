"""Module 7 integration tests for the standardization API surface: the
POST /tasks/{id}/runs source_task_run_id validation extended to
STANDARDIZE tasks, the standardization summary/changes read endpoints,
the approve/reject/rollback state machine (409s on invalid transitions,
tenant isolation on every endpoint), and the two organization-configuration
CRUD endpoint sets (column-mappings, lookup-entries) including their own
tenant isolation and duplicate-scope 409s. Mirrors test_cleaning_api.py's
shape and coverage discipline (Section 12 of the design doc)."""
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
from app.worker.handlers.standardization import StandardizationHandler

CSV_CONTENT = "id,name,email\n1,  jane doe  ,Jane@Example.com\n2,bob smith,BOB@EXAMPLE.COM\n"


def _auth_headers(client: TestClient, suffix: str) -> dict:
    response = client.post(
        "/auth/register",
        json={
            "organization_name": f"Standardization API Org {suffix}",
            "email": f"standardization-api-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Standardization API User",
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


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


def _create_sync_run(client: TestClient, headers: dict, source_id: str) -> str:
    task_response = client.post(
        "/tasks",
        json={"name": "Sync Customers", "task_type": "sync", "data_source_id": source_id},
        headers=headers,
    )
    assert task_response.status_code == 201, task_response.text
    run_response = client.post(f"/tasks/{task_response.json()['id']}/runs", headers=headers)
    assert run_response.status_code == 201, run_response.text
    return run_response.json()["id"]


def _build_approved_standardization_run(
    client: TestClient,
    db_session,
    csv_root: Path,
    headers: dict,
    *,
    relative_path: str = "customers.csv",
) -> tuple[str, str, str]:
    """Full real pipeline through CsvProfilingHandler, CleaningHandler,
    an approve call on the cleaning run, and StandardizationHandler.
    Returns (standardize_task_id, standardize_run_id, organization_id)."""
    source_id = _create_data_source(client, headers, relative_path)

    sync_task_response = client.post(
        "/tasks",
        json={"name": "Sync Customers", "task_type": "sync", "data_source_id": source_id},
        headers=headers,
    )
    sync_task_id = sync_task_response.json()["id"]
    sync_run_response = client.post(f"/tasks/{sync_task_id}/runs", headers=headers)
    sync_run_id = sync_run_response.json()["id"]

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
    transform_task_id = transform_task_response.json()["id"]
    transform_run_response = client.post(
        f"/tasks/{transform_task_id}/runs",
        json={"source_task_run_id": sync_run_id},
        headers=headers,
    )
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
    approve_response = client.post(
        f"/tasks/{transform_task_id}/runs/{transform_run_id}/cleaning/approve", headers=headers
    )
    assert approve_response.status_code == 200, approve_response.text

    standardize_task_response = client.post(
        "/tasks",
        json={
            "name": "Standardize Customers",
            "task_type": "standardize",
            "data_source_id": source_id,
        },
        headers=headers,
    )
    assert standardize_task_response.status_code == 201, standardize_task_response.text
    standardize_task_id = standardize_task_response.json()["id"]
    standardize_run_response = client.post(
        f"/tasks/{standardize_task_id}/runs",
        json={"source_task_run_id": transform_run_id},
        headers=headers,
    )
    assert standardize_run_response.status_code == 201, standardize_run_response.text
    standardize_run_id = standardize_run_response.json()["id"]

    standardize_task = db_session.get(Task, uuid.UUID(standardize_task_id))
    standardize_run = db_session.get(TaskRun, uuid.UUID(standardize_run_id))
    StandardizationHandler().execute(
        ExecutionContext(
            task_run=standardize_run,
            task=standardize_task,
            data_source=source,
            idempotency_key=str(standardize_run.idempotency_key),
            credential_provider=None,
        )
    )
    return standardize_task_id, standardize_run_id, str(source.organization_id)


# --- POST /tasks/{id}/runs: STANDARDIZE source_task_run_id validation ------------


def test_create_run_for_standardize_task_without_source_task_run_id_is_400(
    client: TestClient,
) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    task_response = client.post(
        "/tasks", json={"name": "Standardize", "task_type": "standardize"}, headers=headers
    )
    response = client.post(f"/tasks/{task_response.json()['id']}/runs", headers=headers)
    assert response.status_code == 400, response.text
    assert "source_task_run_id is required" in response.json()["detail"]


def test_create_run_for_standardize_task_with_nonexistent_source_run_is_404(
    client: TestClient,
) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    task_response = client.post(
        "/tasks", json={"name": "Standardize", "task_type": "standardize"}, headers=headers
    )
    response = client.post(
        f"/tasks/{task_response.json()['id']}/runs",
        json={"source_task_run_id": str(uuid.uuid4())},
        headers=headers,
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Source task run not found"


def test_create_run_for_standardize_task_with_cross_org_source_run_is_404(
    client: TestClient,
) -> None:
    owner_headers = _auth_headers(client, uuid.uuid4().hex)
    source_id = _create_data_source(client, owner_headers, "customers.csv")
    sync_run_id = _create_sync_run(client, owner_headers, source_id)

    other_headers = _auth_headers(client, uuid.uuid4().hex)
    task_response = client.post(
        "/tasks", json={"name": "Standardize", "task_type": "standardize"}, headers=other_headers
    )
    response = client.post(
        f"/tasks/{task_response.json()['id']}/runs",
        json={"source_task_run_id": sync_run_id},
        headers=other_headers,
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Source task run not found"


def test_create_run_for_standardize_task_with_valid_source_run_succeeds(
    client: TestClient,
) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    source_id = _create_data_source(client, headers, "customers.csv")
    sync_run_id = _create_sync_run(client, headers, source_id)

    task_response = client.post(
        "/tasks",
        json={"name": "Standardize", "task_type": "standardize", "data_source_id": source_id},
        headers=headers,
    )
    response = client.post(
        f"/tasks/{task_response.json()['id']}/runs",
        json={"source_task_run_id": sync_run_id},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["source_task_run_id"] == sync_run_id


# --- GET .../standardization ------------------------------------------------------


def test_get_task_run_standardization_returns_200_after_successful_execution(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CSV_INPUT_ROOT", str(tmp_path / "csv_in"))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(tmp_path / "csv_out"))
    monkeypatch.setenv("CSV_STANDARDIZED_ROOT", str(tmp_path / "csv_standardized"))
    get_settings.cache_clear()
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        task_id, run_id, _ = _build_approved_standardization_run(
            client, db_session, tmp_path / "csv_in", headers
        )

        response = client.get(f"/tasks/{task_id}/runs/{run_id}/standardization", headers=headers)

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["task_run_id"] == run_id
        assert body["status"] == "pending_review"
        assert body["standardization_engine_version"] == "1.0"
        assert body["total_changes_count"] > 0
    finally:
        get_settings.cache_clear()


def test_get_task_run_standardization_404_when_no_result_yet(client: TestClient) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    source_id = _create_data_source(client, headers, "customers.csv")
    sync_run_id = _create_sync_run(client, headers, source_id)
    task_response = client.post(
        "/tasks",
        json={"name": "Standardize", "task_type": "standardize", "data_source_id": source_id},
        headers=headers,
    )
    run_response = client.post(
        f"/tasks/{task_response.json()['id']}/runs",
        json={"source_task_run_id": sync_run_id},
        headers=headers,
    )

    response = client.get(
        f"/tasks/{task_response.json()['id']}/runs/{run_response.json()['id']}/standardization",
        headers=headers,
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Standardization result not found"


def test_get_task_run_standardization_404_for_cross_org_access(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CSV_INPUT_ROOT", str(tmp_path / "csv_in"))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(tmp_path / "csv_out"))
    monkeypatch.setenv("CSV_STANDARDIZED_ROOT", str(tmp_path / "csv_standardized"))
    get_settings.cache_clear()
    try:
        owner_headers = _auth_headers(client, uuid.uuid4().hex)
        task_id, run_id, _ = _build_approved_standardization_run(
            client, db_session, tmp_path / "csv_in", owner_headers
        )

        other_headers = _auth_headers(client, uuid.uuid4().hex)
        response = client.get(
            f"/tasks/{task_id}/runs/{run_id}/standardization", headers=other_headers
        )
        assert response.status_code == 404, response.text
        assert response.json()["detail"] == "Task not found"
    finally:
        get_settings.cache_clear()


# --- GET .../standardization/changes ----------------------------------------------


def test_list_task_run_standardization_changes_returns_paginated_changes(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CSV_INPUT_ROOT", str(tmp_path / "csv_in"))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(tmp_path / "csv_out"))
    monkeypatch.setenv("CSV_STANDARDIZED_ROOT", str(tmp_path / "csv_standardized"))
    get_settings.cache_clear()
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        task_id, run_id, _ = _build_approved_standardization_run(
            client, db_session, tmp_path / "csv_in", headers
        )

        response = client.get(
            f"/tasks/{task_id}/runs/{run_id}/standardization/changes", headers=headers
        )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["total"] > 0
        assert len(body["items"]) == body["total"]
        for item in body["items"]:
            assert item["field_type"]
            assert item["rule_name"]
            assert item["rule_version"] == "1.0"
            assert item["reason"]
    finally:
        get_settings.cache_clear()


# --- Approval state machine --------------------------------------------------------


def test_approve_transitions_pending_review_to_approved(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CSV_INPUT_ROOT", str(tmp_path / "csv_in"))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(tmp_path / "csv_out"))
    monkeypatch.setenv("CSV_STANDARDIZED_ROOT", str(tmp_path / "csv_standardized"))
    get_settings.cache_clear()
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        task_id, run_id, _ = _build_approved_standardization_run(
            client, db_session, tmp_path / "csv_in", headers
        )

        response = client.post(
            f"/tasks/{task_id}/runs/{run_id}/standardization/approve", headers=headers
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
    monkeypatch.setenv("CSV_STANDARDIZED_ROOT", str(tmp_path / "csv_standardized"))
    get_settings.cache_clear()
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        task_id, run_id, _ = _build_approved_standardization_run(
            client, db_session, tmp_path / "csv_in", headers
        )
        first = client.post(
            f"/tasks/{task_id}/runs/{run_id}/standardization/approve", headers=headers
        )
        assert first.status_code == 200, first.text

        second = client.post(
            f"/tasks/{task_id}/runs/{run_id}/standardization/approve", headers=headers
        )
        assert second.status_code == 409, second.text
        assert "approved" in second.json()["detail"]
    finally:
        get_settings.cache_clear()


def test_reject_transitions_pending_review_to_rejected(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CSV_INPUT_ROOT", str(tmp_path / "csv_in"))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(tmp_path / "csv_out"))
    monkeypatch.setenv("CSV_STANDARDIZED_ROOT", str(tmp_path / "csv_standardized"))
    get_settings.cache_clear()
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        task_id, run_id, _ = _build_approved_standardization_run(
            client, db_session, tmp_path / "csv_in", headers
        )

        response = client.post(
            f"/tasks/{task_id}/runs/{run_id}/standardization/reject", headers=headers
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
    monkeypatch.setenv("CSV_STANDARDIZED_ROOT", str(tmp_path / "csv_standardized"))
    get_settings.cache_clear()
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        task_id, run_id, _ = _build_approved_standardization_run(
            client, db_session, tmp_path / "csv_in", headers
        )
        approve = client.post(
            f"/tasks/{task_id}/runs/{run_id}/standardization/approve", headers=headers
        )
        assert approve.status_code == 200, approve.text

        response = client.post(
            f"/tasks/{task_id}/runs/{run_id}/standardization/reject", headers=headers
        )
        assert response.status_code == 409, response.text
        assert "approved" in response.json()["detail"]
    finally:
        get_settings.cache_clear()


def test_rollback_transitions_approved_to_rolled_back(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CSV_INPUT_ROOT", str(tmp_path / "csv_in"))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(tmp_path / "csv_out"))
    monkeypatch.setenv("CSV_STANDARDIZED_ROOT", str(tmp_path / "csv_standardized"))
    get_settings.cache_clear()
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        task_id, run_id, _ = _build_approved_standardization_run(
            client, db_session, tmp_path / "csv_in", headers
        )
        approve = client.post(
            f"/tasks/{task_id}/runs/{run_id}/standardization/approve", headers=headers
        )
        assert approve.status_code == 200, approve.text

        response = client.post(
            f"/tasks/{task_id}/runs/{run_id}/standardization/rollback", headers=headers
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
    monkeypatch.setenv("CSV_INPUT_ROOT", str(tmp_path / "csv_in"))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(tmp_path / "csv_out"))
    monkeypatch.setenv("CSV_STANDARDIZED_ROOT", str(tmp_path / "csv_standardized"))
    get_settings.cache_clear()
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        task_id, run_id, _ = _build_approved_standardization_run(
            client, db_session, tmp_path / "csv_in", headers
        )

        response = client.post(
            f"/tasks/{task_id}/runs/{run_id}/standardization/rollback", headers=headers
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
    monkeypatch.setenv("CSV_STANDARDIZED_ROOT", str(tmp_path / "csv_standardized"))
    get_settings.cache_clear()
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        task_id, run_id, _ = _build_approved_standardization_run(
            client, db_session, tmp_path / "csv_in", headers
        )
        reject = client.post(
            f"/tasks/{task_id}/runs/{run_id}/standardization/reject", headers=headers
        )
        assert reject.status_code == 200, reject.text

        response = client.post(
            f"/tasks/{task_id}/runs/{run_id}/standardization/rollback", headers=headers
        )
        assert response.status_code == 409, response.text
        assert "rejected" in response.json()["detail"]
    finally:
        get_settings.cache_clear()


def test_rollback_does_not_delete_standardization_changes(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Acceptance Criteria: rollback tested end-to-end and confirmed
    non-destructive -- the StandardizationChange audit trail must remain
    fully readable after a rollback."""
    monkeypatch.setenv("CSV_INPUT_ROOT", str(tmp_path / "csv_in"))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(tmp_path / "csv_out"))
    monkeypatch.setenv("CSV_STANDARDIZED_ROOT", str(tmp_path / "csv_standardized"))
    get_settings.cache_clear()
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        task_id, run_id, _ = _build_approved_standardization_run(
            client, db_session, tmp_path / "csv_in", headers
        )
        before = client.get(
            f"/tasks/{task_id}/runs/{run_id}/standardization/changes", headers=headers
        )
        assert before.status_code == 200
        before_total = before.json()["total"]
        assert before_total > 0

        client.post(f"/tasks/{task_id}/runs/{run_id}/standardization/approve", headers=headers)
        rollback = client.post(
            f"/tasks/{task_id}/runs/{run_id}/standardization/rollback", headers=headers
        )
        assert rollback.status_code == 200, rollback.text

        after = client.get(
            f"/tasks/{task_id}/runs/{run_id}/standardization/changes", headers=headers
        )
        assert after.status_code == 200
        assert after.json()["total"] == before_total
    finally:
        get_settings.cache_clear()


def test_approve_reject_rollback_404_for_cross_org_access(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("CSV_INPUT_ROOT", str(tmp_path / "csv_in"))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(tmp_path / "csv_out"))
    monkeypatch.setenv("CSV_STANDARDIZED_ROOT", str(tmp_path / "csv_standardized"))
    get_settings.cache_clear()
    try:
        owner_headers = _auth_headers(client, uuid.uuid4().hex)
        task_id, run_id, _ = _build_approved_standardization_run(
            client, db_session, tmp_path / "csv_in", owner_headers
        )

        other_headers = _auth_headers(client, uuid.uuid4().hex)
        for action in ("approve", "reject", "rollback"):
            response = client.post(
                f"/tasks/{task_id}/runs/{run_id}/standardization/{action}", headers=other_headers
            )
            assert response.status_code == 404, response.text
            assert response.json()["detail"] == "Task not found"
    finally:
        get_settings.cache_clear()


# --- Standardization configuration: column-mappings ------------------------------


def test_create_column_mapping_succeeds_and_is_org_wide_by_default(client: TestClient) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    response = client.post(
        "/tasks/standardization/column-mappings",
        json={"column_name": "contact_value", "field_type": "person_name"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["column_name"] == "contact_value"
    assert body["field_type"] == "person_name"
    assert body["data_source_id"] is None
    assert body["is_active"] is True


def test_create_column_mapping_rejects_unknown_field_type(client: TestClient) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    response = client.post(
        "/tasks/standardization/column-mappings",
        json={"column_name": "foo", "field_type": "not_a_real_field_type"},
        headers=headers,
    )
    assert response.status_code == 422, response.text


def test_create_column_mapping_rejects_cross_org_data_source_id(client: TestClient) -> None:
    owner_headers = _auth_headers(client, uuid.uuid4().hex)
    source_id = _create_data_source(client, owner_headers, "x.csv")

    other_headers = _auth_headers(client, uuid.uuid4().hex)
    response = client.post(
        "/tasks/standardization/column-mappings",
        json={"data_source_id": source_id, "column_name": "foo", "field_type": "email"},
        headers=other_headers,
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Data source not found"


def test_create_duplicate_active_column_mapping_same_scope_is_409(client: TestClient) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    first = client.post(
        "/tasks/standardization/column-mappings",
        json={"column_name": "contact_value", "field_type": "person_name"},
        headers=headers,
    )
    assert first.status_code == 201, first.text

    second = client.post(
        "/tasks/standardization/column-mappings",
        json={"column_name": "Contact_Value", "field_type": "email"},
        headers=headers,
    )
    assert second.status_code == 409, second.text


def test_list_column_mappings_is_tenant_isolated(client: TestClient) -> None:
    org_a_headers = _auth_headers(client, uuid.uuid4().hex)
    org_b_headers = _auth_headers(client, uuid.uuid4().hex)
    client.post(
        "/tasks/standardization/column-mappings",
        json={"column_name": "only_in_a", "field_type": "person_name"},
        headers=org_a_headers,
    )

    response = client.get("/tasks/standardization/column-mappings", headers=org_b_headers)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0


def test_delete_column_mapping_soft_deletes_and_removes_from_default_listing(
    client: TestClient,
) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    create_response = client.post(
        "/tasks/standardization/column-mappings",
        json={"column_name": "contact_value", "field_type": "person_name"},
        headers=headers,
    )
    mapping_id = create_response.json()["id"]

    delete_response = client.delete(
        f"/tasks/standardization/column-mappings/{mapping_id}", headers=headers
    )
    assert delete_response.status_code == 204, delete_response.text

    active_list = client.get("/tasks/standardization/column-mappings", headers=headers)
    assert active_list.json()["total"] == 0
    full_list = client.get(
        "/tasks/standardization/column-mappings?include_inactive=true", headers=headers
    )
    assert full_list.json()["total"] == 1
    assert full_list.json()["items"][0]["is_active"] is False


def test_delete_column_mapping_404_for_cross_org_access(client: TestClient) -> None:
    owner_headers = _auth_headers(client, uuid.uuid4().hex)
    create_response = client.post(
        "/tasks/standardization/column-mappings",
        json={"column_name": "contact_value", "field_type": "person_name"},
        headers=owner_headers,
    )
    mapping_id = create_response.json()["id"]

    other_headers = _auth_headers(client, uuid.uuid4().hex)
    response = client.delete(
        f"/tasks/standardization/column-mappings/{mapping_id}", headers=other_headers
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Column mapping not found"


# --- Standardization configuration: lookup-entries --------------------------------


def test_create_lookup_entry_succeeds_and_is_global_by_default(client: TestClient) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    response = client.post(
        "/tasks/standardization/lookup-entries",
        json={"lookup_key": "inc", "lookup_value": "Incorporated"},
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["lookup_key"] == "inc"
    assert body["lookup_value"] == "Incorporated"
    assert body["field_type"] is None
    assert body["is_active"] is True


def test_create_lookup_entry_with_field_type_scope_succeeds(client: TestClient) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    response = client.post(
        "/tasks/standardization/lookup-entries",
        json={
            "field_type": "company_name",
            "lookup_key": "acme inc",
            "lookup_value": "Acme Incorporated",
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    assert response.json()["field_type"] == "company_name"


def test_create_lookup_entry_rejects_unknown_field_type(client: TestClient) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    response = client.post(
        "/tasks/standardization/lookup-entries",
        json={"field_type": "not_a_real_field_type", "lookup_key": "x", "lookup_value": "y"},
        headers=headers,
    )
    assert response.status_code == 422, response.text


def test_create_duplicate_active_lookup_entry_same_scope_is_409(client: TestClient) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    first = client.post(
        "/tasks/standardization/lookup-entries",
        json={"lookup_key": "inc", "lookup_value": "Incorporated"},
        headers=headers,
    )
    assert first.status_code == 201, first.text

    second = client.post(
        "/tasks/standardization/lookup-entries",
        json={"lookup_key": "Inc", "lookup_value": "Different Value"},
        headers=headers,
    )
    assert second.status_code == 409, second.text


def test_list_lookup_entries_is_tenant_isolated(client: TestClient) -> None:
    org_a_headers = _auth_headers(client, uuid.uuid4().hex)
    org_b_headers = _auth_headers(client, uuid.uuid4().hex)
    client.post(
        "/tasks/standardization/lookup-entries",
        json={"lookup_key": "only_in_a", "lookup_value": "value"},
        headers=org_a_headers,
    )

    response = client.get("/tasks/standardization/lookup-entries", headers=org_b_headers)
    assert response.status_code == 200, response.text
    assert response.json()["total"] == 0


def test_delete_lookup_entry_soft_deletes_and_removes_from_default_listing(
    client: TestClient,
) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    create_response = client.post(
        "/tasks/standardization/lookup-entries",
        json={"lookup_key": "inc", "lookup_value": "Incorporated"},
        headers=headers,
    )
    entry_id = create_response.json()["id"]

    delete_response = client.delete(
        f"/tasks/standardization/lookup-entries/{entry_id}", headers=headers
    )
    assert delete_response.status_code == 204, delete_response.text

    active_list = client.get("/tasks/standardization/lookup-entries", headers=headers)
    assert active_list.json()["total"] == 0
    full_list = client.get(
        "/tasks/standardization/lookup-entries?include_inactive=true", headers=headers
    )
    assert full_list.json()["total"] == 1
    assert full_list.json()["items"][0]["is_active"] is False


def test_delete_lookup_entry_404_for_cross_org_access(client: TestClient) -> None:
    owner_headers = _auth_headers(client, uuid.uuid4().hex)
    create_response = client.post(
        "/tasks/standardization/lookup-entries",
        json={"lookup_key": "inc", "lookup_value": "Incorporated"},
        headers=owner_headers,
    )
    entry_id = create_response.json()["id"]

    other_headers = _auth_headers(client, uuid.uuid4().hex)
    response = client.delete(
        f"/tasks/standardization/lookup-entries/{entry_id}", headers=other_headers
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Lookup entry not found"
