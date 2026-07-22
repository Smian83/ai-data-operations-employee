"""Module 11 API-level tests for GET /review-queue. The heavy aggregation/
classification/filter/sort/summary correctness is already covered directly
against fetch_review_queue() in test_review_queue_query.py (unit-testable
without a running API, per the approved design) -- this file exercises
the HTTP boundary itself: auth, tenant isolation via real requests,
query-param validation, response-schema shape, and that a real
pending_review item produced by the actual worker pipeline is correctly
surfaced. Mirrors this suite's established per-file self-contained
pipeline-building convention (test_artifact_download_api.py,
test_artifact_download_constraints.py) rather than importing helpers from
another test file."""
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.models.cleaning_run import CleaningRun
from app.models.data_source import DataSource
from app.models.task import Task
from app.models.task_run import TaskRun
from app.worker.handlers.base import ExecutionContext
from app.worker.handlers.cleaning import CleaningHandler
from app.worker.handlers.csv_profiling import CsvProfilingHandler


def _load_cleaning_run(db_session, task_run_id: str) -> CleaningRun:
    """CleaningRun's own primary key is NOT the id POST /tasks/{id}/runs
    returns (that's the TaskRun's own id) -- must be resolved via
    task_run_id, mirroring _get_cleaning_run_or_404's own lookup pattern
    in app/api/tasks.py (the same distinction test_artifact_download_api.py
    documents and fixes for exactly this reason)."""
    return db_session.execute(
        select(CleaningRun).where(CleaningRun.task_run_id == uuid.UUID(task_run_id))
    ).scalar_one()

CSV_CONTENT = (
    "id,name,email\n"
    "1,jane doe,jane@example.com\n"
    "2,bob smith,bob@example.com\n"
    "2,bob smith,bob@example.com\n"
    "3,mary jones,mary@example.com\n"
)


def _auth_headers(client: TestClient, suffix: str) -> dict:
    response = client.post(
        "/auth/register",
        json={
            "organization_name": f"Review Queue API Org {suffix}",
            "email": f"review-queue-api-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Review Queue API User",
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
    monkeypatch.setenv("CSV_EXPORTED_ROOT", str(tmp_path / "csv_exported"))
    get_settings.cache_clear()
    return csv_root


def _build_one_pending_review_cleaning_run(client, db_session, csv_root, headers, task_name="Clean"):
    """SYNC -> TRANSFORM, deliberately left at pending_review (no approve
    call) -- the minimal real, worker-produced item needed to smoke-test
    the HTTP boundary."""
    source_response = client.post(
        "/data-sources",
        json={
            "name": "Uploaded Customers", "source_type": "csv_upload",
            "connection_metadata": {"file_path": "customers.csv"},
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
    (org_dir / "customers.csv").write_text(CSV_CONTENT, encoding="utf-8")

    source = db_session.get(DataSource, uuid.UUID(source_id))
    sync_task = db_session.get(Task, uuid.UUID(sync_task_response.json()["id"]))
    sync_run = db_session.get(TaskRun, uuid.UUID(sync_run_id))
    CsvProfilingHandler().execute(
        ExecutionContext(
            task_run=sync_run, task=sync_task, data_source=source,
            idempotency_key=str(sync_run.idempotency_key), credential_provider=None,
        )
    )

    clean_task_response = client.post(
        "/tasks", json={"name": task_name, "task_type": "transform", "data_source_id": source_id},
        headers=headers,
    )
    clean_task_id = clean_task_response.json()["id"]
    clean_run_response = client.post(
        f"/tasks/{clean_task_id}/runs", json={"source_task_run_id": sync_run_id}, headers=headers
    )
    clean_run_id = clean_run_response.json()["id"]
    clean_task = db_session.get(Task, uuid.UUID(clean_task_id))
    clean_run = db_session.get(TaskRun, uuid.UUID(clean_run_id))
    CleaningHandler().execute(
        ExecutionContext(
            task_run=clean_run, task=clean_task, data_source=source,
            idempotency_key=str(clean_run.idempotency_key), credential_provider=None,
        )
    )
    return {"organization_id": organization_id, "clean_task_id": clean_task_id, "clean_run_id": clean_run_id}


def test_review_queue_requires_authentication(client: TestClient):
    response = client.get("/review-queue")
    assert response.status_code == 401


def test_review_queue_surfaces_real_pending_review_cleaning_run(client, db_session, monkeypatch, tmp_path):
    csv_root = _set_roots(monkeypatch, tmp_path)
    headers = _auth_headers(client, uuid.uuid4().hex)
    ids = _build_one_pending_review_cleaning_run(client, db_session, csv_root, headers)

    response = client.get("/review-queue", headers=headers)
    assert response.status_code == 200
    body = response.json()

    assert set(body.keys()) == {"items", "total", "limit", "offset", "summary"}
    assert set(body["summary"].keys()) == {
        "total_items", "pending_reviews", "ambiguous_matches", "failed_runs", "download_failures",
    }
    assert body["total"] == 1
    assert body["summary"]["pending_reviews"] == 1
    item = body["items"][0]
    assert item["review_category"] == "PROCESSING"
    assert item["review_type"] == "PENDING_REVIEW"
    assert item["source"] == "cleaning_run"
    cleaning_run = _load_cleaning_run(db_session, ids["clean_run_id"])
    assert item["reference_id"] == str(cleaning_run.id)
    assert item["organization_id"] == ids["organization_id"]
    # No priority field anywhere in the response contract (Revision 3
    # removed it entirely -- not merely left NULL).
    assert "priority" not in item


def test_review_queue_tenant_isolation_via_api(client, db_session, monkeypatch, tmp_path):
    csv_root = _set_roots(monkeypatch, tmp_path)
    headers_a = _auth_headers(client, uuid.uuid4().hex)
    headers_b = _auth_headers(client, uuid.uuid4().hex)
    ids_a = _build_one_pending_review_cleaning_run(client, db_session, csv_root, headers_a, task_name="Clean A")

    response_b = client.get("/review-queue", headers=headers_b)
    assert response_b.status_code == 200
    assert response_b.json()["total"] == 0

    response_a = client.get("/review-queue", headers=headers_a)
    assert response_a.status_code == 200
    assert response_a.json()["total"] == 1
    cleaning_run_a = _load_cleaning_run(db_session, ids_a["clean_run_id"])
    assert response_a.json()["items"][0]["reference_id"] == str(cleaning_run_a.id)


def test_review_queue_invalid_category_returns_422(client: TestClient):
    headers = _auth_headers(client, uuid.uuid4().hex)
    response = client.get("/review-queue", params={"review_category": "NOT_REAL"}, headers=headers)
    assert response.status_code == 422


def test_review_queue_invalid_type_returns_422(client: TestClient):
    headers = _auth_headers(client, uuid.uuid4().hex)
    response = client.get("/review-queue", params={"review_type": "NOT_REAL"}, headers=headers)
    assert response.status_code == 422


def test_review_queue_invalid_sort_returns_422(client: TestClient):
    headers = _auth_headers(client, uuid.uuid4().hex)
    response = client.get("/review-queue", params={"sort": "priority"}, headers=headers)
    assert response.status_code == 422


def test_review_queue_valid_category_filter_via_api(client, db_session, monkeypatch, tmp_path):
    csv_root = _set_roots(monkeypatch, tmp_path)
    headers = _auth_headers(client, uuid.uuid4().hex)
    _build_one_pending_review_cleaning_run(client, db_session, csv_root, headers)

    response = client.get("/review-queue", params={"review_category": "PROCESSING"}, headers=headers)
    assert response.status_code == 200
    assert response.json()["total"] == 1

    response_empty = client.get("/review-queue", params={"review_category": "EXPORT"}, headers=headers)
    assert response_empty.status_code == 200
    assert response_empty.json()["total"] == 0


def test_review_queue_pagination_params_via_api(client, db_session, monkeypatch, tmp_path):
    csv_root = _set_roots(monkeypatch, tmp_path)
    headers = _auth_headers(client, uuid.uuid4().hex)
    _build_one_pending_review_cleaning_run(client, db_session, csv_root, headers)

    response = client.get("/review-queue", params={"limit": 1, "offset": 0}, headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 1
    assert body["offset"] == 0
    assert len(body["items"]) == 1
