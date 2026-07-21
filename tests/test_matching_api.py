"""Module 8 integration tests for the matching API surface: the
POST /tasks/{id}/runs source_task_run_id validation extended to MATCH
tasks, the matching summary/groups/decisions/skipped-blocks read
endpoints (including the ?decision=/?match_group_id=/?blocking_key=
filters added in the approved design revision), the approve/reject/
rollback state machine (409s on invalid transitions, tenant isolation on
every endpoint), and the match-rule-set configuration CRUD endpoints
(create/list, tenant isolation, threshold-order 422, duplicate-column
422, version-supersession). Mirrors test_standardization_api.py's shape
and coverage discipline."""
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
from app.worker.handlers.matching import MatchHandler
from app.worker.handlers.standardization import StandardizationHandler

CSV_CONTENT = (
    "id,name,email\n"
    "1,  jane doe  ,Jane@Example.com\n"
    "2,bob smith,BOB@EXAMPLE.COM\n"
)


def _auth_headers(client: TestClient, suffix: str) -> dict:
    response = client.post(
        "/auth/register",
        json={
            "organization_name": f"Matching API Org {suffix}",
            "email": f"matching-api-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Matching API User",
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _set_roots(monkeypatch, tmp_path: Path) -> Path:
    csv_root = tmp_path / "csv_in"
    csv_root.mkdir()
    monkeypatch.setenv("CSV_INPUT_ROOT", str(csv_root))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(tmp_path / "csv_out"))
    monkeypatch.setenv("CSV_STANDARDIZED_ROOT", str(tmp_path / "csv_standardized"))
    get_settings.cache_clear()
    return csv_root


def _build_approved_standardization_run(
    client: TestClient,
    db_session,
    csv_root: Path,
    headers: dict,
    *,
    relative_path: str = "customers.csv",
) -> tuple[str, str, str]:
    """Returns (source_id, standardize_run_id, organization_id)."""
    source_response = client.post(
        "/data-sources",
        json={
            "name": "Uploaded Customers", "source_type": "csv_upload",
            "connection_metadata": {"file_path": relative_path},
        },
        headers=headers,
    )
    source_id = source_response.json()["id"]
    organization_id = source_response.json()["organization_id"]

    sync_task_response = client.post(
        "/tasks", json={"name": "Sync", "task_type": "sync", "data_source_id": source_id},
        headers=headers,
    )
    sync_run_response = client.post(f"/tasks/{sync_task_response.json()['id']}/runs", headers=headers)
    sync_run_id = sync_run_response.json()["id"]

    org_dir = csv_root / organization_id
    org_dir.mkdir(parents=True, exist_ok=True)
    (org_dir / relative_path).write_text(CSV_CONTENT, encoding="utf-8")

    source = db_session.get(DataSource, uuid.UUID(source_id))
    sync_task = db_session.get(Task, uuid.UUID(sync_task_response.json()["id"]))
    sync_run = db_session.get(TaskRun, uuid.UUID(sync_run_id))
    CsvProfilingHandler().execute(
        ExecutionContext(
            task_run=sync_run, task=sync_task, data_source=source,
            idempotency_key=str(sync_run.idempotency_key), credential_provider=None,
        )
    )

    transform_task_response = client.post(
        "/tasks",
        json={"name": "Clean", "task_type": "transform", "data_source_id": source_id},
        headers=headers,
    )
    transform_task_id = transform_task_response.json()["id"]
    transform_run_response = client.post(
        f"/tasks/{transform_task_id}/runs", json={"source_task_run_id": sync_run_id}, headers=headers
    )
    transform_run_id = transform_run_response.json()["id"]
    transform_task = db_session.get(Task, uuid.UUID(transform_task_id))
    transform_run = db_session.get(TaskRun, uuid.UUID(transform_run_id))
    CleaningHandler().execute(
        ExecutionContext(
            task_run=transform_run, task=transform_task, data_source=source,
            idempotency_key=str(transform_run.idempotency_key), credential_provider=None,
        )
    )
    approve = client.post(
        f"/tasks/{transform_task_id}/runs/{transform_run_id}/cleaning/approve", headers=headers
    )
    assert approve.status_code == 200, approve.text

    standardize_task_response = client.post(
        "/tasks",
        json={"name": "Standardize", "task_type": "standardize", "data_source_id": source_id},
        headers=headers,
    )
    standardize_task_id = standardize_task_response.json()["id"]
    standardize_run_response = client.post(
        f"/tasks/{standardize_task_id}/runs",
        json={"source_task_run_id": transform_run_id},
        headers=headers,
    )
    standardize_run_id = standardize_run_response.json()["id"]
    standardize_task = db_session.get(Task, uuid.UUID(standardize_task_id))
    standardize_run = db_session.get(TaskRun, uuid.UUID(standardize_run_id))
    StandardizationHandler().execute(
        ExecutionContext(
            task_run=standardize_run, task=standardize_task, data_source=source,
            idempotency_key=str(standardize_run.idempotency_key), credential_provider=None,
        )
    )
    approve2 = client.post(
        f"/tasks/{standardize_task_id}/runs/{standardize_run_id}/standardization/approve",
        headers=headers,
    )
    assert approve2.status_code == 200, approve2.text

    return source_id, standardize_run_id, organization_id


def _build_completed_match_run(
    client: TestClient, db_session, csv_root: Path, headers: dict
) -> tuple[str, str]:
    """Returns (match_task_id, match_run_id) for a fully executed MATCH run."""
    source_id, standardize_run_id, _ = _build_approved_standardization_run(
        client, db_session, csv_root, headers
    )
    match_task_response = client.post(
        "/tasks", json={"name": "Match", "task_type": "match", "data_source_id": source_id},
        headers=headers,
    )
    assert match_task_response.status_code == 201, match_task_response.text
    match_task_id = match_task_response.json()["id"]
    match_run_response = client.post(
        f"/tasks/{match_task_id}/runs",
        json={"source_task_run_id": standardize_run_id},
        headers=headers,
    )
    assert match_run_response.status_code == 201, match_run_response.text
    match_run_id = match_run_response.json()["id"]

    match_task = db_session.get(Task, uuid.UUID(match_task_id))
    match_run = db_session.get(TaskRun, uuid.UUID(match_run_id))
    source = db_session.get(DataSource, uuid.UUID(source_id))
    MatchHandler().execute(
        ExecutionContext(
            task_run=match_run, task=match_task, data_source=source,
            idempotency_key=str(match_run.idempotency_key), credential_provider=None,
        )
    )
    return match_task_id, match_run_id


# --- POST /tasks/{id}/runs: MATCH source_task_run_id validation --------------


def test_create_run_for_match_task_without_source_task_run_id_is_400(client: TestClient) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    task_response = client.post("/tasks", json={"name": "Match", "task_type": "match"}, headers=headers)
    response = client.post(f"/tasks/{task_response.json()['id']}/runs", headers=headers)
    assert response.status_code == 400, response.text
    assert "source_task_run_id is required" in response.json()["detail"]


def test_create_run_for_match_task_with_unknown_source_task_run_id_is_404(client: TestClient) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    task_response = client.post("/tasks", json={"name": "Match", "task_type": "match"}, headers=headers)
    response = client.post(
        f"/tasks/{task_response.json()['id']}/runs",
        json={"source_task_run_id": str(uuid.uuid4())},
        headers=headers,
    )
    assert response.status_code == 404, response.text


def test_create_run_for_match_task_with_valid_source_task_run_id_is_201(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        _, standardize_run_id, _ = _build_approved_standardization_run(
            client, db_session, csv_root, headers
        )
        task_response = client.post(
            "/tasks", json={"name": "Match", "task_type": "match"}, headers=headers
        )
        response = client.post(
            f"/tasks/{task_response.json()['id']}/runs",
            json={"source_task_run_id": standardize_run_id},
            headers=headers,
        )
        assert response.status_code == 201, response.text
    finally:
        get_settings.cache_clear()


def test_source_task_run_id_rejected_for_sync_task(client: TestClient) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    task_response = client.post("/tasks", json={"name": "Sync", "task_type": "sync"}, headers=headers)
    response = client.post(
        f"/tasks/{task_response.json()['id']}/runs",
        json={"source_task_run_id": str(uuid.uuid4())},
        headers=headers,
    )
    assert response.status_code == 400, response.text
    assert "only valid for TRANSFORM, STANDARDIZE, and MATCH" in response.json()["detail"]


# --- GET .../matching summary/groups/decisions/skipped-blocks ---------------


def test_get_matching_summary_and_lists(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        match_task_id, match_run_id = _build_completed_match_run(client, db_session, csv_root, headers)

        summary = client.get(f"/tasks/{match_task_id}/runs/{match_run_id}/matching", headers=headers)
        assert summary.status_code == 200, summary.text
        assert summary.json()["status"] == "pending_review"
        assert "output_file_path" not in summary.json()

        groups = client.get(
            f"/tasks/{match_task_id}/runs/{match_run_id}/matching/groups", headers=headers
        )
        assert groups.status_code == 200, groups.text

        decisions = client.get(
            f"/tasks/{match_task_id}/runs/{match_run_id}/matching/decisions", headers=headers
        )
        assert decisions.status_code == 200, decisions.text

        skipped = client.get(
            f"/tasks/{match_task_id}/runs/{match_run_id}/matching/skipped-blocks", headers=headers
        )
        assert skipped.status_code == 200, skipped.text
        assert skipped.json()["items"] == []
    finally:
        get_settings.cache_clear()


def test_get_matching_decisions_invalid_decision_filter_is_400(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        match_task_id, match_run_id = _build_completed_match_run(client, db_session, csv_root, headers)
        response = client.get(
            f"/tasks/{match_task_id}/runs/{match_run_id}/matching/decisions",
            params={"decision": "not_a_real_value"},
            headers=headers,
        )
        assert response.status_code == 400, response.text
    finally:
        get_settings.cache_clear()


def test_get_matching_summary_404_for_nonexistent_run(client: TestClient) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    task_response = client.post("/tasks", json={"name": "Match", "task_type": "match"}, headers=headers)
    response = client.get(
        f"/tasks/{task_response.json()['id']}/runs/{uuid.uuid4()}/matching", headers=headers
    )
    assert response.status_code == 404, response.text


def test_matching_endpoints_are_tenant_isolated(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers_a = _auth_headers(client, uuid.uuid4().hex)
        match_task_id, match_run_id = _build_completed_match_run(client, db_session, csv_root, headers_a)

        headers_b = _auth_headers(client, uuid.uuid4().hex)
        response = client.get(
            f"/tasks/{match_task_id}/runs/{match_run_id}/matching", headers=headers_b
        )
        assert response.status_code == 404, response.text
    finally:
        get_settings.cache_clear()


# --- Approval state machine --------------------------------------------------


def test_matching_approve_reject_rollback_state_machine(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        match_task_id, match_run_id = _build_completed_match_run(client, db_session, csv_root, headers)

        # Cannot reject after approving.
        approve = client.post(
            f"/tasks/{match_task_id}/runs/{match_run_id}/matching/approve", headers=headers
        )
        assert approve.status_code == 200, approve.text
        assert approve.json()["status"] == "approved"

        reject_after_approve = client.post(
            f"/tasks/{match_task_id}/runs/{match_run_id}/matching/reject", headers=headers
        )
        assert reject_after_approve.status_code == 409, reject_after_approve.text

        rollback = client.post(
            f"/tasks/{match_task_id}/runs/{match_run_id}/matching/rollback", headers=headers
        )
        assert rollback.status_code == 200, rollback.text
        assert rollback.json()["status"] == "rolled_back"

        # Cannot roll back twice.
        rollback_again = client.post(
            f"/tasks/{match_task_id}/runs/{match_run_id}/matching/rollback", headers=headers
        )
        assert rollback_again.status_code == 409, rollback_again.text
    finally:
        get_settings.cache_clear()


def test_matching_reject_then_approve_is_409(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        match_task_id, match_run_id = _build_completed_match_run(client, db_session, csv_root, headers)

        reject = client.post(
            f"/tasks/{match_task_id}/runs/{match_run_id}/matching/reject", headers=headers
        )
        assert reject.status_code == 200, reject.text

        approve_after_reject = client.post(
            f"/tasks/{match_task_id}/runs/{match_run_id}/matching/approve", headers=headers
        )
        assert approve_after_reject.status_code == 409, approve_after_reject.text
    finally:
        get_settings.cache_clear()


def test_matching_rollback_before_approval_is_409(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        match_task_id, match_run_id = _build_completed_match_run(client, db_session, csv_root, headers)

        rollback = client.post(
            f"/tasks/{match_task_id}/runs/{match_run_id}/matching/rollback", headers=headers
        )
        assert rollback.status_code == 409, rollback.text
    finally:
        get_settings.cache_clear()


def test_matching_approve_is_cross_org_404(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers_a = _auth_headers(client, uuid.uuid4().hex)
        match_task_id, match_run_id = _build_completed_match_run(client, db_session, csv_root, headers_a)

        headers_b = _auth_headers(client, uuid.uuid4().hex)
        response = client.post(
            f"/tasks/{match_task_id}/runs/{match_run_id}/matching/approve", headers=headers_b
        )
        assert response.status_code == 404, response.text
    finally:
        get_settings.cache_clear()


# --- Match rule-set configuration CRUD ---------------------------------------


def test_create_match_rule_set_success(client: TestClient) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    response = client.post(
        "/tasks/matching/rule-sets",
        json={
            "duplicate_threshold": 0.9, "review_threshold": 0.4,
            "fields": [
                {"column_name": "email", "comparison_type": "normalized_exact", "weight": 1.0},
                {"column_name": "name", "comparison_type": "normalized_exact", "weight": 0.3},
            ],
        },
        headers=headers,
    )
    assert response.status_code == 201, response.text
    body = response.json()
    assert body["version"] == 1
    assert body["is_active"] is True
    assert len(body["fields"]) == 2


def test_create_match_rule_set_invalid_threshold_order_is_422(client: TestClient) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    response = client.post(
        "/tasks/matching/rule-sets",
        json={
            "duplicate_threshold": 0.4, "review_threshold": 0.9,
            "fields": [{"column_name": "email", "comparison_type": "normalized_exact", "weight": 1.0}],
        },
        headers=headers,
    )
    assert response.status_code == 422, response.text


def test_create_match_rule_set_invalid_comparison_type_is_422(client: TestClient) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    response = client.post(
        "/tasks/matching/rule-sets",
        json={
            "duplicate_threshold": 0.9, "review_threshold": 0.4,
            "fields": [{"column_name": "email", "comparison_type": "fuzzy_match", "weight": 1.0}],
        },
        headers=headers,
    )
    assert response.status_code == 422, response.text


def test_create_match_rule_set_duplicate_column_is_422(client: TestClient) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    response = client.post(
        "/tasks/matching/rule-sets",
        json={
            "duplicate_threshold": 0.9, "review_threshold": 0.4,
            "fields": [
                {"column_name": "email", "comparison_type": "normalized_exact", "weight": 1.0},
                {"column_name": "Email", "comparison_type": "exact", "weight": 0.5},
            ],
        },
        headers=headers,
    )
    assert response.status_code == 422, response.text


def test_create_match_rule_set_empty_fields_is_422(client: TestClient) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    response = client.post(
        "/tasks/matching/rule-sets",
        json={"duplicate_threshold": 0.9, "review_threshold": 0.4, "fields": []},
        headers=headers,
    )
    assert response.status_code == 422, response.text


def test_creating_a_new_org_wide_rule_set_deactivates_the_prior_one(client: TestClient) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    payload = {
        "duplicate_threshold": 0.9, "review_threshold": 0.4,
        "fields": [{"column_name": "email", "comparison_type": "normalized_exact", "weight": 1.0}],
    }
    first = client.post("/tasks/matching/rule-sets", json=payload, headers=headers)
    assert first.status_code == 201, first.text
    second = client.post("/tasks/matching/rule-sets", json=payload, headers=headers)
    assert second.status_code == 201, second.text
    assert second.json()["version"] == 2

    active_list = client.get("/tasks/matching/rule-sets", headers=headers)
    assert active_list.status_code == 200, active_list.text
    active_ids = {row["id"] for row in active_list.json()["items"]}
    assert active_ids == {second.json()["id"]}

    all_list = client.get(
        "/tasks/matching/rule-sets", params={"include_inactive": True}, headers=headers
    )
    assert len(all_list.json()["items"]) == 2


def test_match_rule_sets_are_tenant_isolated(client: TestClient) -> None:
    headers_a = _auth_headers(client, uuid.uuid4().hex)
    payload = {
        "duplicate_threshold": 0.9, "review_threshold": 0.4,
        "fields": [{"column_name": "email", "comparison_type": "normalized_exact", "weight": 1.0}],
    }
    created = client.post("/tasks/matching/rule-sets", json=payload, headers=headers_a)
    assert created.status_code == 201, created.text

    headers_b = _auth_headers(client, uuid.uuid4().hex)
    listing = client.get("/tasks/matching/rule-sets", headers=headers_b)
    assert listing.status_code == 200, listing.text
    assert listing.json()["items"] == []
