"""Module 9 integration tests for the export API surface: the
POST /tasks/{id}/runs source_task_run_id validation extended to EXPORT
tasks, the export summary/exclusions read endpoints (including the
?match_group_id= filter), the approve/reject/rollback state machine
(409s on invalid transitions, tenant isolation on every endpoint), and
the file-metadata fields added per architectural review. Mirrors
test_matching_api.py's shape and coverage discipline."""
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.export.engine import RESERVED_CANONICAL_RECORD_COLUMN
from app.models.data_source import DataSource
from app.models.export_run import ExportRun
from app.models.task import Task
from app.models.task_run import TaskRun
from app.worker.handlers.base import ExecutionContext
from app.worker.handlers.cleaning import CleaningHandler
from app.worker.handlers.csv_profiling import CsvProfilingHandler
from app.worker.handlers.export import ExportHandler
from app.worker.handlers.matching import MatchHandler
from app.worker.handlers.standardization import StandardizationHandler

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
            "organization_name": f"Export API Org {suffix}",
            "email": f"export-api-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Export API User",
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


def _build_approved_match_run(
    client: TestClient, db_session, csv_root: Path, headers: dict
) -> tuple[str, str, str]:
    """Returns (source_id, match_run_id, organization_id) for a fully
    executed and APPROVED MATCH run."""
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

    match_task_response = client.post(
        "/tasks", json={"name": "Match", "task_type": "match", "data_source_id": source_id},
        headers=headers,
    )
    match_task_id = match_task_response.json()["id"]
    match_run_response = client.post(
        f"/tasks/{match_task_id}/runs",
        json={"source_task_run_id": standardize_run_id},
        headers=headers,
    )
    match_run_id = match_run_response.json()["id"]
    match_task = db_session.get(Task, uuid.UUID(match_task_id))
    match_run = db_session.get(TaskRun, uuid.UUID(match_run_id))
    MatchHandler().execute(
        ExecutionContext(
            task_run=match_run, task=match_task, data_source=source,
            idempotency_key=str(match_run.idempotency_key), credential_provider=None,
        )
    )
    approve3 = client.post(
        f"/tasks/{match_task_id}/runs/{match_run_id}/matching/approve", headers=headers
    )
    assert approve3.status_code == 200, approve3.text

    return source_id, match_run_id, organization_id


def _build_completed_export_run(
    client: TestClient, db_session, csv_root: Path, headers: dict
) -> tuple[str, str]:
    """Returns (export_task_id, export_run_id) for a fully executed EXPORT run."""
    source_id, match_run_id, _ = _build_approved_match_run(client, db_session, csv_root, headers)
    export_task_response = client.post(
        "/tasks",
        json={"name": "Export", "task_type": "export", "data_source_id": source_id},
        headers=headers,
    )
    assert export_task_response.status_code == 201, export_task_response.text
    export_task_id = export_task_response.json()["id"]
    export_run_response = client.post(
        f"/tasks/{export_task_id}/runs",
        json={"source_task_run_id": match_run_id},
        headers=headers,
    )
    assert export_run_response.status_code == 201, export_run_response.text
    export_run_id = export_run_response.json()["id"]

    export_task = db_session.get(Task, uuid.UUID(export_task_id))
    export_run = db_session.get(TaskRun, uuid.UUID(export_run_id))
    source = db_session.get(DataSource, uuid.UUID(source_id))
    ExportHandler().execute(
        ExecutionContext(
            task_run=export_run, task=export_task, data_source=source,
            idempotency_key=str(export_run.idempotency_key), credential_provider=None,
        )
    )
    return export_task_id, export_run_id


# --- POST /tasks/{id}/runs: EXPORT source_task_run_id validation ------------


def test_create_run_for_export_task_without_source_task_run_id_is_400(client: TestClient) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    task_response = client.post("/tasks", json={"name": "Export", "task_type": "export"}, headers=headers)
    response = client.post(f"/tasks/{task_response.json()['id']}/runs", headers=headers)
    assert response.status_code == 400, response.text
    assert "source_task_run_id is required" in response.json()["detail"]
    assert "EXPORT" in response.json()["detail"]


def test_create_run_for_export_task_with_unknown_source_task_run_id_is_404(client: TestClient) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    task_response = client.post("/tasks", json={"name": "Export", "task_type": "export"}, headers=headers)
    response = client.post(
        f"/tasks/{task_response.json()['id']}/runs",
        json={"source_task_run_id": str(uuid.uuid4())},
        headers=headers,
    )
    assert response.status_code == 404, response.text


def test_create_run_for_export_task_with_valid_source_task_run_id_is_201(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        _, match_run_id, _ = _build_approved_match_run(client, db_session, csv_root, headers)
        task_response = client.post(
            "/tasks", json={"name": "Export", "task_type": "export"}, headers=headers
        )
        response = client.post(
            f"/tasks/{task_response.json()['id']}/runs",
            json={"source_task_run_id": match_run_id},
            headers=headers,
        )
        assert response.status_code == 201, response.text
    finally:
        get_settings.cache_clear()


# --- GET .../export summary/exclusions ---------------------------------------


def test_get_export_summary_and_exclusions(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        export_task_id, export_run_id = _build_completed_export_run(client, db_session, csv_root, headers)

        summary = client.get(f"/tasks/{export_task_id}/runs/{export_run_id}/export", headers=headers)
        assert summary.status_code == 200, summary.text
        body = summary.json()
        assert body["status"] == "pending_review"
        assert body["source_row_count"] == 4
        assert body["row_count"] == 3
        assert body["excluded_row_count"] == 1
        assert body["duplicate_groups_materialized_count"] == 1
        assert body["csv_format_version"] == 1
        assert body["output_column_count"] == 5
        # Module 10: output_file_path is deliberately absent from the API
        # response (removed per architectural review -- see
        # docs/module-10-artifact-retrieval-design.md Section 13);
        # output_sha256 remains, since a content hash is not a
        # filesystem detail.
        assert "output_file_path" not in body
        assert "output_sha256" in body
        assert "export_timestamp" in body

        exclusions = client.get(
            f"/tasks/{export_task_id}/runs/{export_run_id}/export/exclusions", headers=headers
        )
        assert exclusions.status_code == 200, exclusions.text
        assert exclusions.json()["total"] == 1
        exclusion = exclusions.json()["items"][0]
        assert exclusion["row_index"] == 2
        assert "canonical_row_index" in exclusion
        assert "reason" in exclusion

        filtered = client.get(
            f"/tasks/{export_task_id}/runs/{export_run_id}/export/exclusions",
            params={"match_group_id": exclusion["match_group_id"]},
            headers=headers,
        )
        assert filtered.status_code == 200, filtered.text
        assert filtered.json()["total"] == 1

        filtered_miss = client.get(
            f"/tasks/{export_task_id}/runs/{export_run_id}/export/exclusions",
            params={"match_group_id": str(uuid.uuid4())},
            headers=headers,
        )
        assert filtered_miss.status_code == 200, filtered_miss.text
        assert filtered_miss.json()["total"] == 0
    finally:
        get_settings.cache_clear()


def test_get_export_summary_404_for_nonexistent_run(client: TestClient) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    task_response = client.post("/tasks", json={"name": "Export", "task_type": "export"}, headers=headers)
    response = client.get(
        f"/tasks/{task_response.json()['id']}/runs/{uuid.uuid4()}/export", headers=headers
    )
    assert response.status_code == 404, response.text


def test_export_endpoints_are_tenant_isolated(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers_a = _auth_headers(client, uuid.uuid4().hex)
        export_task_id, export_run_id = _build_completed_export_run(
            client, db_session, csv_root, headers_a
        )

        headers_b = _auth_headers(client, uuid.uuid4().hex)
        response = client.get(
            f"/tasks/{export_task_id}/runs/{export_run_id}/export", headers=headers_b
        )
        assert response.status_code == 404, response.text

        exclusions_response = client.get(
            f"/tasks/{export_task_id}/runs/{export_run_id}/export/exclusions", headers=headers_b
        )
        assert exclusions_response.status_code == 404, exclusions_response.text

        approve_response = client.post(
            f"/tasks/{export_task_id}/runs/{export_run_id}/export/approve", headers=headers_b
        )
        assert approve_response.status_code == 404, approve_response.text
    finally:
        get_settings.cache_clear()


# --- Approval state machine --------------------------------------------------


def test_export_approve_reject_rollback_state_machine(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        export_task_id, export_run_id = _build_completed_export_run(client, db_session, csv_root, headers)

        # Cannot reject after approving.
        approve = client.post(
            f"/tasks/{export_task_id}/runs/{export_run_id}/export/approve", headers=headers
        )
        assert approve.status_code == 200, approve.text
        assert approve.json()["status"] == "approved"
        assert approve.json()["approved_by"] is not None

        reject_after_approve = client.post(
            f"/tasks/{export_task_id}/runs/{export_run_id}/export/reject", headers=headers
        )
        assert reject_after_approve.status_code == 409, reject_after_approve.text

        # Cannot approve twice.
        approve_again = client.post(
            f"/tasks/{export_task_id}/runs/{export_run_id}/export/approve", headers=headers
        )
        assert approve_again.status_code == 409, approve_again.text

        rollback = client.post(
            f"/tasks/{export_task_id}/runs/{export_run_id}/export/rollback", headers=headers
        )
        assert rollback.status_code == 200, rollback.text
        assert rollback.json()["status"] == "rolled_back"

        # Cannot roll back twice.
        rollback_again = client.post(
            f"/tasks/{export_task_id}/runs/{export_run_id}/export/rollback", headers=headers
        )
        assert rollback_again.status_code == 409, rollback_again.text
    finally:
        get_settings.cache_clear()


def test_export_reject_then_cannot_approve_or_rollback(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        export_task_id, export_run_id = _build_completed_export_run(client, db_session, csv_root, headers)

        reject = client.post(
            f"/tasks/{export_task_id}/runs/{export_run_id}/export/reject", headers=headers
        )
        assert reject.status_code == 200, reject.text
        assert reject.json()["status"] == "rejected"

        approve_after_reject = client.post(
            f"/tasks/{export_task_id}/runs/{export_run_id}/export/approve", headers=headers
        )
        assert approve_after_reject.status_code == 409, approve_after_reject.text

        rollback_after_reject = client.post(
            f"/tasks/{export_task_id}/runs/{export_run_id}/export/rollback", headers=headers
        )
        assert rollback_after_reject.status_code == 409, rollback_after_reject.text
    finally:
        get_settings.cache_clear()


def test_export_rollback_does_not_delete_the_output_file(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        export_task_id, export_run_id = _build_completed_export_run(client, db_session, csv_root, headers)

        # Module 10: output_file_path is no longer in the API response
        # (Section 13); fetch it via the ORM row directly, the same
        # pattern every other test in this suite that needs the
        # server-local path already uses.
        # export_run_id here is the TaskRun id (the URL's run_id) --
        # ExportRun is looked up by task_run_id, mirroring
        # _get_export_run_or_404's own lookup in app.api.tasks.
        export_run = db_session.execute(
            select(ExportRun).where(ExportRun.task_run_id == uuid.UUID(export_run_id))
        ).scalar_one()
        output_path = Path(export_run.output_file_path)
        assert output_path.exists()

        client.post(f"/tasks/{export_task_id}/runs/{export_run_id}/export/approve", headers=headers)
        client.post(f"/tasks/{export_task_id}/runs/{export_run_id}/export/rollback", headers=headers)

        assert output_path.exists()
    finally:
        get_settings.cache_clear()


def test_exported_csv_header_never_contains_a_reserved_column_from_input(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        export_task_id, export_run_id = _build_completed_export_run(client, db_session, csv_root, headers)
        # Module 10: output_file_path is no longer in the API response
        # (Section 13); fetch it via the ORM row directly.
        # export_run_id here is the TaskRun id (the URL's run_id) --
        # ExportRun is looked up by task_run_id, mirroring
        # _get_export_run_or_404's own lookup in app.api.tasks.
        export_run = db_session.execute(
            select(ExportRun).where(ExportRun.task_run_id == uuid.UUID(export_run_id))
        ).scalar_one()
        output_path = Path(export_run.output_file_path)
        header = output_path.read_text(encoding="utf-8").splitlines()[0]
        assert header.count(RESERVED_CANONICAL_RECORD_COLUMN) == 1
    finally:
        get_settings.cache_clear()
