"""Module 10 integration tests for the artifact-download API surface:
the three download endpoints (cleaning/standardization/export), the
approved/rolled_back/pending_review/rejected downloadable-state policy,
tenant isolation, the verify-before-stream integrity guarantee (zero
bytes on a hash mismatch), the file-missing and mid-stream-failure
outcomes, the ArtifactDownloadEvent audit lifecycle (exactly one row
per authorized attempt, never updated twice, no row for unauthorized
attempts), the X-Artifact-Run-Status header, and output_file_path's
removal from all three *Read summary responses. Mirrors
test_export_api.py's shape and fixture-building discipline (each API
test file in this suite builds its own self-contained pipeline
helpers rather than importing another file's)."""
import hashlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.models.artifact_download_event import ArtifactDownloadEvent
from app.models.cleaning_run import CleaningRun
from app.models.data_source import DataSource
from app.models.export_run import ExportRun
from app.models.standardization_run import StandardizationRun
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
            "organization_name": f"Download API Org {suffix}",
            "email": f"download-api-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Download API User",
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


def _build_pipeline(client: TestClient, db_session, csv_root: Path, headers: dict) -> dict:
    """Builds a full SYNC -> TRANSFORM(clean, auto-approved) ->
    STANDARDIZE(auto-approved) -> MATCH(auto-approved) -> EXPORT
    (pending_review) chain, identical in shape to test_export_api.py's
    _build_approved_match_run/_build_completed_export_run, extended to
    also return the cleaning/standardization task+run ids (already
    approved by the time the chain reaches export) so a single pipeline
    build can exercise all three download endpoints."""
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
        "/tasks", json={"name": "Clean", "task_type": "transform", "data_source_id": source_id},
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
    approve_clean = client.post(f"/tasks/{clean_task_id}/runs/{clean_run_id}/cleaning/approve", headers=headers)
    assert approve_clean.status_code == 200, approve_clean.text

    std_task_response = client.post(
        "/tasks",
        json={"name": "Standardize", "task_type": "standardize", "data_source_id": source_id},
        headers=headers,
    )
    std_task_id = std_task_response.json()["id"]
    std_run_response = client.post(
        f"/tasks/{std_task_id}/runs", json={"source_task_run_id": clean_run_id}, headers=headers
    )
    std_run_id = std_run_response.json()["id"]
    std_task = db_session.get(Task, uuid.UUID(std_task_id))
    std_run = db_session.get(TaskRun, uuid.UUID(std_run_id))
    StandardizationHandler().execute(
        ExecutionContext(
            task_run=std_run, task=std_task, data_source=source,
            idempotency_key=str(std_run.idempotency_key), credential_provider=None,
        )
    )
    approve_std = client.post(
        f"/tasks/{std_task_id}/runs/{std_run_id}/standardization/approve", headers=headers
    )
    assert approve_std.status_code == 200, approve_std.text

    match_task_response = client.post(
        "/tasks", json={"name": "Match", "task_type": "match", "data_source_id": source_id},
        headers=headers,
    )
    match_task_id = match_task_response.json()["id"]
    match_run_response = client.post(
        f"/tasks/{match_task_id}/runs", json={"source_task_run_id": std_run_id}, headers=headers
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
    approve_match = client.post(f"/tasks/{match_task_id}/runs/{match_run_id}/matching/approve", headers=headers)
    assert approve_match.status_code == 200, approve_match.text

    export_task_response = client.post(
        "/tasks", json={"name": "Export", "task_type": "export", "data_source_id": source_id},
        headers=headers,
    )
    export_task_id = export_task_response.json()["id"]
    export_run_response = client.post(
        f"/tasks/{export_task_id}/runs", json={"source_task_run_id": match_run_id}, headers=headers
    )
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

    return {
        "organization_id": organization_id,
        "clean_task_id": clean_task_id, "clean_run_id": clean_run_id,
        "std_task_id": std_task_id, "std_run_id": std_run_id,
        "export_task_id": export_task_id, "export_run_id": export_run_id,
    }


def _load_export_run(db_session, task_run_id: str) -> ExportRun:
    """ExportRun is looked up by task_run_id, not by its own id -- the
    'run_id' in every /tasks/{task_id}/runs/{run_id}/... URL (and in the
    ids dict _build_pipeline returns) IS the TaskRun id, mirroring
    _get_export_run_or_404's own lookup in app.api.tasks."""
    return db_session.execute(
        select(ExportRun).where(ExportRun.task_run_id == uuid.UUID(task_run_id))
    ).scalar_one()


def _load_cleaning_run(db_session, task_run_id: str) -> CleaningRun:
    return db_session.execute(
        select(CleaningRun).where(CleaningRun.task_run_id == uuid.UUID(task_run_id))
    ).scalar_one()


def _load_standardization_run(db_session, task_run_id: str) -> StandardizationRun:
    return db_session.execute(
        select(StandardizationRun).where(
            StandardizationRun.task_run_id == uuid.UUID(task_run_id)
        )
    ).scalar_one()


def _events_for_export_run(db_session, export_task_run_id: str) -> list[ArtifactDownloadEvent]:
    """export_task_run_id is the TaskRun id (the 'run_id' used in URLs
    and in the ids dict _build_pipeline returns), NOT ExportRun's own
    primary key -- resolved here via ExportRun.task_run_id, mirroring
    _get_export_run_or_404's own lookup, since
    ArtifactDownloadEvent.export_run_id references ExportRun.id."""
    export_run_pk = db_session.execute(
        select(ExportRun.id).where(ExportRun.task_run_id == uuid.UUID(export_task_run_id))
    ).scalar_one_or_none()
    if export_run_pk is None:
        return []
    return list(
        db_session.execute(
            select(ArtifactDownloadEvent).where(
                ArtifactDownloadEvent.export_run_id == export_run_pk
            )
        ).scalars().all()
    )


# --- success paths -----------------------------------------------------


def test_download_export_artifact_success(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        approve = client.post(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/approve",
            headers=headers,
        )
        assert approve.status_code == 200, approve.text

        export_run = _load_export_run(db_session, ids["export_run_id"])
        on_disk = Path(export_run.output_file_path).read_bytes()

        response = client.get(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/download",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.content == on_disk
        assert response.headers["x-artifact-run-status"] == "approved"
        assert response.headers["content-disposition"] == (
            f'attachment; filename="export-{export_run.id}.csv"'
        )
        assert response.headers["content-length"] == str(len(on_disk))
        # Never a real filesystem path anywhere in the response headers.
        assert str(export_run.output_file_path) not in response.headers["content-disposition"]

        db_session.expire_all()
        events = _events_for_export_run(db_session, ids["export_run_id"])
        assert len(events) == 1
        event = events[0]
        assert event.outcome == "completed"
        assert event.artifact_type == "export"
        assert event.run_status_at_request == "approved"
        assert event.bytes_served == len(on_disk)
        assert event.verified_sha256 == export_run.output_sha256
        assert event.failure_reason_code is None
        assert event.completed_at is not None
        assert event.cleaning_run_id is None
        assert event.standardization_run_id is None
    finally:
        get_settings.cache_clear()


def test_download_cleaning_artifact_success(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        cleaning_run = _load_cleaning_run(db_session, ids["clean_run_id"])
        on_disk = Path(cleaning_run.output_file_path).read_bytes()

        response = client.get(
            f"/tasks/{ids['clean_task_id']}/runs/{ids['clean_run_id']}/cleaning/download",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.content == on_disk
        assert response.headers["x-artifact-run-status"] == "approved"

        db_session.expire_all()
        events = list(
            db_session.execute(
                select(ArtifactDownloadEvent).where(
                    ArtifactDownloadEvent.cleaning_run_id == cleaning_run.id
                )
            ).scalars().all()
        )
        assert len(events) == 1
        assert events[0].outcome == "completed"
        assert events[0].artifact_type == "cleaning"
    finally:
        get_settings.cache_clear()


def test_download_standardization_artifact_success(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        std_run = _load_standardization_run(db_session, ids["std_run_id"])
        on_disk = Path(std_run.output_file_path).read_bytes()

        response = client.get(
            f"/tasks/{ids['std_task_id']}/runs/{ids['std_run_id']}/standardization/download",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.content == on_disk

        db_session.expire_all()
        events = list(
            db_session.execute(
                select(ArtifactDownloadEvent).where(
                    ArtifactDownloadEvent.standardization_run_id == std_run.id
                )
            ).scalars().all()
        )
        assert len(events) == 1
        assert events[0].outcome == "completed"
        assert events[0].artifact_type == "standardization"
    finally:
        get_settings.cache_clear()


def test_download_exactly_one_new_audit_row_per_attempt_never_reused(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        client.post(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/approve",
            headers=headers,
        )
        url = f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/download"

        first = client.get(url, headers=headers)
        second = client.get(url, headers=headers)
        assert first.status_code == 200
        assert second.status_code == 200
        # Content retrieval is deterministic/repeatable...
        assert first.content == second.content

        db_session.expire_all()
        events = _events_for_export_run(db_session, ids["export_run_id"])
        # ...but the audit side effect is intentionally non-idempotent:
        # each authorized attempt gets its own row, never reused.
        assert len(events) == 2
        assert events[0].id != events[1].id
        assert all(e.outcome == "completed" for e in events)
    finally:
        get_settings.cache_clear()


# --- downloadable-state policy ------------------------------------------


def test_download_pending_review_export_is_409_and_creates_no_audit_row(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)

        response = client.get(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/download",
            headers=headers,
        )
        assert response.status_code == 409, response.text

        events = _events_for_export_run(db_session, ids["export_run_id"])
        assert events == []
    finally:
        get_settings.cache_clear()


def test_download_rejected_export_is_409_and_creates_no_audit_row(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        reject = client.post(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/reject",
            headers=headers,
        )
        assert reject.status_code == 200, reject.text

        response = client.get(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/download",
            headers=headers,
        )
        assert response.status_code == 409, response.text
        assert _events_for_export_run(db_session, ids["export_run_id"]) == []
    finally:
        get_settings.cache_clear()


def test_download_rolled_back_export_succeeds_with_rolled_back_header(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        client.post(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/approve",
            headers=headers,
        )
        rollback = client.post(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/rollback",
            headers=headers,
        )
        assert rollback.status_code == 200, rollback.text

        response = client.get(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/download",
            headers=headers,
        )
        assert response.status_code == 200, response.text
        assert response.headers["x-artifact-run-status"] == "rolled_back"

        db_session.expire_all()
        events = _events_for_export_run(db_session, ids["export_run_id"])
        assert len(events) == 1
        assert events[0].run_status_at_request == "rolled_back"
        assert events[0].outcome == "completed"
    finally:
        get_settings.cache_clear()


# --- tenant isolation ----------------------------------------------------


def test_download_cross_organization_is_404_and_creates_no_audit_row(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        client.post(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/approve",
            headers=headers,
        )

        other_headers = _auth_headers(client, uuid.uuid4().hex)
        response = client.get(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/download",
            headers=other_headers,
        )
        assert response.status_code == 404, response.text
        assert _events_for_export_run(db_session, ids["export_run_id"]) == []
    finally:
        get_settings.cache_clear()


# --- integrity / missing-file / stream failure paths ----------------------


def test_download_missing_file_returns_404_and_records_file_missing(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        client.post(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/approve",
            headers=headers,
        )
        export_run = _load_export_run(db_session, ids["export_run_id"])
        Path(export_run.output_file_path).unlink()

        response = client.get(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/download",
            headers=headers,
        )
        assert response.status_code == 404, response.text
        # Indistinguishable from a nonexistent run -- same detail text
        # a 404 from _get_export_run_or_404 would use elsewhere.
        assert "output_file_path" not in response.text
        assert str(export_run.output_file_path) not in response.text

        db_session.expire_all()
        events = _events_for_export_run(db_session, ids["export_run_id"])
        assert len(events) == 1
        assert events[0].outcome == "file_missing"
        assert events[0].failure_reason_code == "file_not_found"
        assert events[0].bytes_served == 0
        assert events[0].verified_sha256 is None
        assert events[0].completed_at is not None
    finally:
        get_settings.cache_clear()


def test_download_corrupted_artifact_sends_zero_bytes_and_records_integrity_failed(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        client.post(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/approve",
            headers=headers,
        )
        export_run = _load_export_run(db_session, ids["export_run_id"])
        original = Path(export_run.output_file_path).read_bytes()
        tampered = original + b"tampered extra bytes not covered by the recorded hash\n"
        Path(export_run.output_file_path).write_bytes(tampered)

        response = client.get(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/download",
            headers=headers,
        )
        # NO ARTIFACT BYTES ARE SENT BEFORE INTEGRITY VERIFICATION
        # SUCCEEDS -- the failure is a plain JSON error, structurally
        # incapable of containing any CSV bytes (raised before the
        # StreamingResponse is ever constructed).
        assert response.status_code == 500, response.text
        assert response.headers["content-type"].startswith("application/json")
        assert tampered not in response.content
        assert original not in response.content
        # No hash values and no filesystem path leaked to the client.
        assert export_run.output_sha256 not in response.text
        assert hashlib.sha256(tampered).hexdigest() not in response.text
        assert str(export_run.output_file_path) not in response.text

        db_session.expire_all()
        events = _events_for_export_run(db_session, ids["export_run_id"])
        assert len(events) == 1
        assert events[0].outcome == "integrity_failed"
        assert events[0].failure_reason_code == "hash_mismatch"
        assert events[0].bytes_served == 0
        assert events[0].verified_sha256 is None
        assert events[0].completed_at is not None
    finally:
        get_settings.cache_clear()


def test_download_mid_stream_failure_records_stream_failed_with_partial_bytes(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        client.post(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/approve",
            headers=headers,
        )

        import app.api.tasks as tasks_module

        real_iter = tasks_module.iter_artifact_chunks

        def _flaky_iter(fileobj):
            gen = real_iter(fileobj)
            first_chunk = next(gen)
            yield first_chunk
            raise RuntimeError("simulated mid-stream I/O failure")

        monkeypatch.setattr(tasks_module, "iter_artifact_chunks", _flaky_iter)

        # Starlette's TestClient (via anyio's portal) may surface a
        # mid-stream exception wrapped in an ExceptionGroup rather than
        # the bare RuntimeError, depending on the installed anyio/
        # exceptiongroup versions -- assert on the underlying message
        # rather than a specific exception class.
        with pytest.raises(Exception) as exc_info:
            client.get(
                f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/download",
                headers=headers,
            )
        assert "simulated mid-stream I/O failure" in str(exc_info.value) or any(
            "simulated mid-stream I/O failure" in str(sub)
            for sub in getattr(exc_info.value, "exceptions", [])
        )

        db_session.expire_all()
        events = _events_for_export_run(db_session, ids["export_run_id"])
        assert len(events) == 1
        assert events[0].outcome == "stream_failed"
        assert events[0].failure_reason_code == "stream_interrupted"
        assert events[0].bytes_served > 0
        # Verification itself succeeded before the failure -- the hash
        # is still recorded even though the transfer did not complete.
        assert events[0].verified_sha256 is not None
        assert events[0].completed_at is not None
    finally:
        get_settings.cache_clear()


def test_failure_after_verification_before_streaming_records_stream_failed(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Regression test (final review finding): between a successful
    open_verified_artifact() call and the StreamingResponse actually
    being constructed, _download_artifact calls os.fstat() to compute
    Content-Length. If that call fails, the already-open, already-
    verified file descriptor must still be closed and the audit row
    must still reach a terminal outcome -- not leak the descriptor or
    leave the row stuck at 'started' forever.

    The fstat() patch below only intercepts the exact file descriptor
    opened for this download (captured by wrapping
    open_verified_artifact) and delegates every other fd to the real
    os.fstat -- so this cannot collaterally break unrelated fstat calls
    made elsewhere during the same request (e.g. by the DB driver)."""
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        client.post(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/approve",
            headers=headers,
        )

        import app.api.tasks as tasks_module

        real_open_verified_artifact = tasks_module.open_verified_artifact
        opened_fileobjs = []

        def _capturing_open_verified_artifact(path, expected_sha256):
            fileobj = real_open_verified_artifact(path, expected_sha256)
            opened_fileobjs.append(fileobj)
            return fileobj

        monkeypatch.setattr(
            tasks_module, "open_verified_artifact", _capturing_open_verified_artifact
        )

        real_fstat = tasks_module.os.fstat

        def _flaky_fstat(fd, *args, **kwargs):
            if opened_fileobjs and fd == opened_fileobjs[0].fileno():
                raise OSError("simulated fstat failure")
            return real_fstat(fd, *args, **kwargs)

        monkeypatch.setattr(tasks_module.os, "fstat", _flaky_fstat)

        response = client.get(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/download",
            headers=headers,
        )
        assert response.status_code == 500, response.text
        assert response.headers["content-type"].startswith("application/json")

        assert opened_fileobjs, "open_verified_artifact was never called"
        assert opened_fileobjs[0].closed, "file descriptor was leaked after fstat() failure"

        db_session.expire_all()
        events = _events_for_export_run(db_session, ids["export_run_id"])
        assert len(events) == 1
        assert events[0].outcome == "stream_failed"
        assert events[0].failure_reason_code == "io_error"
        assert events[0].bytes_served == 0
        # Verification succeeded before the fstat() failure -- the hash
        # is still recorded.
        assert events[0].verified_sha256 is not None
        assert events[0].completed_at is not None
    finally:
        get_settings.cache_clear()


# --- purged artifact (Module 13 Phase 4) ----------------------------------


def test_download_purged_export_artifact_returns_410_and_records_purged_event(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        client.post(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/approve",
            headers=headers,
        )
        export_run = _load_export_run(db_session, ids["export_run_id"])
        export_run.output_deleted_at = datetime.now(timezone.utc)
        db_session.commit()

        response = client.get(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/download",
            headers=headers,
        )
        assert response.status_code == 410, response.text
        assert response.json()["detail"] == "Artifact no longer available"

        db_session.expire_all()
        events = _events_for_export_run(db_session, ids["export_run_id"])
        assert len(events) == 1
        assert events[0].outcome == "purged"
        assert events[0].artifact_type == "export"
        assert events[0].failure_reason_code is None
        assert events[0].bytes_served == 0
        assert events[0].verified_sha256 is None
        assert events[0].completed_at is not None
        assert events[0].run_status_at_request == "approved"
    finally:
        get_settings.cache_clear()


def test_download_purged_cleaning_artifact_returns_410_and_records_purged_event(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        cleaning_run = _load_cleaning_run(db_session, ids["clean_run_id"])
        cleaning_run.output_deleted_at = datetime.now(timezone.utc)
        db_session.commit()

        response = client.get(
            f"/tasks/{ids['clean_task_id']}/runs/{ids['clean_run_id']}/cleaning/download",
            headers=headers,
        )
        assert response.status_code == 410, response.text

        db_session.expire_all()
        events = list(
            db_session.execute(
                select(ArtifactDownloadEvent).where(
                    ArtifactDownloadEvent.cleaning_run_id == cleaning_run.id
                )
            ).scalars().all()
        )
        assert len(events) == 1
        assert events[0].outcome == "purged"
        assert events[0].artifact_type == "cleaning"
        assert events[0].failure_reason_code is None
        assert events[0].completed_at is not None
    finally:
        get_settings.cache_clear()


def test_download_purged_standardization_artifact_returns_410_and_records_purged_event(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        std_run = _load_standardization_run(db_session, ids["std_run_id"])
        std_run.output_deleted_at = datetime.now(timezone.utc)
        db_session.commit()

        response = client.get(
            f"/tasks/{ids['std_task_id']}/runs/{ids['std_run_id']}/standardization/download",
            headers=headers,
        )
        assert response.status_code == 410, response.text

        db_session.expire_all()
        events = list(
            db_session.execute(
                select(ArtifactDownloadEvent).where(
                    ArtifactDownloadEvent.standardization_run_id == std_run.id
                )
            ).scalars().all()
        )
        assert len(events) == 1
        assert events[0].outcome == "purged"
        assert events[0].artifact_type == "standardization"
        assert events[0].failure_reason_code is None
        assert events[0].completed_at is not None
    finally:
        get_settings.cache_clear()


def test_download_purged_response_never_exposes_path_or_deleted_at(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        client.post(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/approve",
            headers=headers,
        )
        export_run = _load_export_run(db_session, ids["export_run_id"])
        deleted_at = datetime.now(timezone.utc)
        export_run.output_deleted_at = deleted_at
        db_session.commit()
        output_path = str(export_run.output_file_path)

        response = client.get(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/download",
            headers=headers,
        )
        assert response.status_code == 410, response.text
        body_text = response.text
        assert output_path not in body_text
        assert str(export_run.id) not in body_text or "detail" in response.json()
        # The generic message only -- no ISO timestamp, no path fragment,
        # no internal exception text of any kind.
        assert response.json() == {"detail": "Artifact no longer available"}
        for header_value in response.headers.values():
            assert output_path not in header_value
    finally:
        get_settings.cache_clear()


def test_download_purged_check_precedes_path_resolution_and_storage_access(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Regression test for the exact ordering this phase's instructions
    require: a purged artifact must be rejected before
    resolve_artifact_path or ArtifactStorage.open is ever reached. Proven
    here by monkeypatching resolve_artifact_path (as imported into
    app.api.tasks) to raise if called at all -- if the 410 branch is ever
    reordered to run after path resolution, this test fails loudly
    instead of silently passing for the wrong reason."""
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        client.post(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/approve",
            headers=headers,
        )
        export_run = _load_export_run(db_session, ids["export_run_id"])
        export_run.output_deleted_at = datetime.now(timezone.utc)
        db_session.commit()

        import app.api.tasks as tasks_module

        def _must_not_be_called(*args, **kwargs):
            raise AssertionError(
                "resolve_artifact_path was called for a purged artifact -- "
                "the 410 check must short-circuit before this point"
            )

        monkeypatch.setattr(tasks_module, "resolve_artifact_path", _must_not_be_called)

        response = client.get(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/download",
            headers=headers,
        )
        assert response.status_code == 410, response.text
    finally:
        get_settings.cache_clear()


# --- output_file_path removal ---------------------------------------------


def test_output_file_path_absent_from_all_three_summary_responses(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)

        cleaning_summary = client.get(
            f"/tasks/{ids['clean_task_id']}/runs/{ids['clean_run_id']}/cleaning", headers=headers
        )
        std_summary = client.get(
            f"/tasks/{ids['std_task_id']}/runs/{ids['std_run_id']}/standardization", headers=headers
        )
        export_summary = client.get(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export", headers=headers
        )
        for summary in (cleaning_summary, std_summary, export_summary):
            assert summary.status_code == 200, summary.text
            body = summary.json()
            assert "output_file_path" not in body
            assert "output_sha256" in body
    finally:
        get_settings.cache_clear()
