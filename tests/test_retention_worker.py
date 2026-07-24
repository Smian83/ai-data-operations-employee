"""Module 13 tests for app.worker.retention.purge_expired_artifacts: the
four-state artifact lifecycle's eligibility rules, per-outcome audit
behavior (completed/already_missing/failed, real and dry-run), path
safety, and idempotency. Builds a real Sync -> Clean(approved) ->
Standardize(approved) -> Match(approved) -> Export(pending_review)
pipeline against real files on disk, mirroring
test_artifact_download_api.py's own self-contained fixture-building
discipline for this suite -- most tests then manipulate one run's
status/decision-timestamp/output_deleted_at directly via db_session to
reach the exact state under test, since the retention pass itself is a
worker-internal function with no HTTP surface of its own.

Real PostgreSQL concurrency is covered separately in
tests/test_retention_concurrency.py, following the same
SQLite-cannot-prove-this precedent as
tests/test_scheduled_tasks_concurrency.py."""
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select, text

from app.core.config import get_settings
from app.models.artifact_retention_event import ArtifactRetentionEvent
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
from app.worker.retention import purge_expired_artifacts

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
            "organization_name": f"Retention Org {suffix}",
            "email": f"retention-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Retention Test User",
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


def _enable_retention(
    monkeypatch, *, window_days: int = 7, dry_run: bool = False, batch_size: int = 50
) -> None:
    monkeypatch.setenv("OUTPUT_RETENTION_ENABLED", "true")
    monkeypatch.setenv("OUTPUT_RETENTION_WINDOW_DAYS", str(window_days))
    monkeypatch.setenv("OUTPUT_RETENTION_DRY_RUN", "true" if dry_run else "false")
    monkeypatch.setenv("RETENTION_CLAIM_BATCH_SIZE", str(batch_size))
    get_settings.cache_clear()


def _build_pipeline(client: TestClient, db_session, csv_root: Path, headers: dict) -> dict:
    """Sync -> Clean(auto-approved) -> Standardize(auto-approved) ->
    Match(auto-approved) -> Export(left pending_review). Identical shape
    to test_artifact_download_api.py's own _build_pipeline."""
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


def _load_cleaning_run(db_session, task_run_id: str) -> CleaningRun:
    return db_session.execute(
        select(CleaningRun).where(CleaningRun.task_run_id == uuid.UUID(task_run_id))
    ).scalar_one()


def _load_standardization_run(db_session, task_run_id: str) -> StandardizationRun:
    return db_session.execute(
        select(StandardizationRun).where(StandardizationRun.task_run_id == uuid.UUID(task_run_id))
    ).scalar_one()


def _load_export_run(db_session, task_run_id: str) -> ExportRun:
    return db_session.execute(
        select(ExportRun).where(ExportRun.task_run_id == uuid.UUID(task_run_id))
    ).scalar_one()


def _events_for_cleaning_run(db_session, cleaning_run_id) -> list[ArtifactRetentionEvent]:
    return list(
        db_session.execute(
            select(ArtifactRetentionEvent).where(
                ArtifactRetentionEvent.cleaning_run_id == cleaning_run_id
            )
        ).scalars().all()
    )


def _ago(days: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


# --- disabled-by-default safety --------------------------------------------


def test_retention_disabled_by_default_touches_nothing(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        cleaning_run = _load_cleaning_run(db_session, ids["clean_run_id"])
        cleaning_run.approved_at = _ago(30)
        db_session.commit()

        # OUTPUT_RETENTION_ENABLED left at its default (false) -- never
        # explicitly enabled in this test.
        result = purge_expired_artifacts(db_session)

        assert result.purged_count == 0
        assert result.already_missing_count == 0
        assert result.failed_count == 0
        assert result.dry_run_would_purge_count == 0
        assert result.candidates_considered == 0
        assert Path(cleaning_run.output_file_path).exists()
        db_session.refresh(cleaning_run)
        assert cleaning_run.output_deleted_at is None
        assert _events_for_cleaning_run(db_session, cleaning_run.id) == []
    finally:
        get_settings.cache_clear()


# --- eligibility ------------------------------------------------------------


def test_pending_review_never_eligible_even_with_a_stray_decision_timestamp(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Defensive: even if a pending_review row somehow carried a non-NULL
    approved_at (never possible through the real API, but not something
    the eligibility query should trust blindly), the CASE expression only
    ever looks at approved_at when status='approved' -- status is what
    gates eligibility, not the mere presence of a timestamp."""
    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        export_run = _load_export_run(db_session, ids["export_run_id"])
        assert export_run.status == "pending_review"
        export_run.approved_at = _ago(30)  # stray/corrupted value
        db_session.commit()

        result = purge_expired_artifacts(db_session)

        assert result.candidates_considered == 0
        db_session.refresh(export_run)
        assert export_run.output_deleted_at is None
        assert Path(export_run.output_file_path).exists()
    finally:
        get_settings.cache_clear()


def test_terminal_but_inside_window_not_eligible(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        cleaning_run = _load_cleaning_run(db_session, ids["clean_run_id"])
        cleaning_run.approved_at = _ago(2)  # well inside the 7-day window
        db_session.commit()

        result = purge_expired_artifacts(db_session)

        assert result.candidates_considered == 0
        db_session.refresh(cleaning_run)
        assert cleaning_run.output_deleted_at is None
        assert Path(cleaning_run.output_file_path).exists()
    finally:
        get_settings.cache_clear()


def test_approved_past_cutoff_is_eligible_and_purged(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        cleaning_run = _load_cleaning_run(db_session, ids["clean_run_id"])
        cleaning_run.approved_at = _ago(10)  # past the 7-day cutoff
        db_session.commit()
        output_path = Path(cleaning_run.output_file_path)
        assert output_path.exists()

        result = purge_expired_artifacts(db_session)

        assert result.purged_count == 1
        assert result.candidates_considered == 1
        assert not output_path.exists()
        db_session.refresh(cleaning_run)
        assert cleaning_run.output_deleted_at is not None
        # Historical record fields are never cleared.
        assert cleaning_run.output_file_path == str(output_path)
        assert cleaning_run.output_sha256 is not None
    finally:
        get_settings.cache_clear()


def test_rejected_past_cutoff_is_eligible_and_uses_rejected_at(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        reject = client.post(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/reject",
            headers=headers,
        )
        assert reject.status_code == 200, reject.text
        export_run = _load_export_run(db_session, ids["export_run_id"])
        assert export_run.status == "rejected"
        export_run.rejected_at = _ago(10)
        db_session.commit()
        output_path = Path(export_run.output_file_path)

        result = purge_expired_artifacts(db_session)

        assert result.purged_count == 1
        db_session.refresh(export_run)
        assert export_run.output_deleted_at is not None
        assert not output_path.exists()
    finally:
        get_settings.cache_clear()


def test_rolled_back_uses_rolled_back_at_not_an_older_approved_at(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """A rolled_back run always uses rolled_back_at, never an older
    approved_at from the same row -- proven both directions: a recent
    rolled_back_at (inside the window) keeps the artifact ACTIVE even
    though approved_at is ancient, and an old rolled_back_at makes it
    EXPIRED regardless of how old approved_at is."""
    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        approve = client.post(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/approve",
            headers=headers,
        )
        assert approve.status_code == 200, approve.text
        rollback = client.post(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/rollback",
            headers=headers,
        )
        assert rollback.status_code == 200, rollback.text

        export_run = _load_export_run(db_session, ids["export_run_id"])
        assert export_run.status == "rolled_back"
        # approved_at is ancient (would make it eligible if the query
        # mistakenly used the earliest timestamp); rolled_back_at is
        # recent (inside the window) -- must NOT be eligible.
        export_run.approved_at = _ago(365)
        export_run.rolled_back_at = _ago(2)
        db_session.commit()

        result_inside_window = purge_expired_artifacts(db_session)
        assert result_inside_window.candidates_considered == 0
        db_session.refresh(export_run)
        assert export_run.output_deleted_at is None
        assert Path(export_run.output_file_path).exists()

        # Now push rolled_back_at itself past the cutoff -- must become
        # eligible, using rolled_back_at (not the even-older approved_at).
        export_run.rolled_back_at = _ago(10)
        db_session.commit()

        result_past_cutoff = purge_expired_artifacts(db_session)
        assert result_past_cutoff.purged_count == 1
        db_session.refresh(export_run)
        assert export_run.output_deleted_at is not None
    finally:
        get_settings.cache_clear()


def test_output_deleted_at_already_set_is_never_reprocessed(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        cleaning_run = _load_cleaning_run(db_session, ids["clean_run_id"])
        cleaning_run.approved_at = _ago(10)
        cleaning_run.output_deleted_at = _ago(1)  # already purged previously
        db_session.commit()

        result = purge_expired_artifacts(db_session)

        assert result.candidates_considered == 0
        assert _events_for_cleaning_run(db_session, cleaning_run.id) == []
    finally:
        get_settings.cache_clear()


def test_missing_matching_decision_timestamp_not_eligible(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Defensive: status='approved' but approved_at somehow NULL (bypasses
    the ORM/application layer via a direct UPDATE) -- the eligibility
    query's decision_ts is NULL in this case and structurally excludes
    the row, exactly like a pending_review row would be excluded."""
    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        cleaning_run = _load_cleaning_run(db_session, ids["clean_run_id"])
        assert cleaning_run.status == "approved"
        db_session.execute(
            text("UPDATE cleaning_runs SET approved_at = NULL WHERE id = :id"),
            {"id": str(cleaning_run.id)},
        )
        db_session.commit()

        result = purge_expired_artifacts(db_session)

        assert result.candidates_considered == 0
    finally:
        get_settings.cache_clear()


# --- outcomes: real pass -----------------------------------------------------


def test_successful_deletion_records_completed_with_size(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        cleaning_run = _load_cleaning_run(db_session, ids["clean_run_id"])
        cleaning_run.approved_at = _ago(10)
        db_session.commit()
        output_path = Path(cleaning_run.output_file_path)
        original_size = output_path.stat().st_size

        result = purge_expired_artifacts(db_session)

        assert result.purged_count == 1
        assert result.bytes_reclaimed == original_size
        events = _events_for_cleaning_run(db_session, cleaning_run.id)
        assert len(events) == 1
        event = events[0]
        assert event.outcome == "completed"
        assert event.dry_run is False
        assert event.failure_reason_code is None
        assert event.artifact_size_bytes == original_size
        assert event.retention_window_days_applied == 7
        assert event.completed_at is not None
    finally:
        get_settings.cache_clear()


def test_already_missing_file_records_already_missing_and_sets_output_deleted_at(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        cleaning_run = _load_cleaning_run(db_session, ids["clean_run_id"])
        cleaning_run.approved_at = _ago(10)
        db_session.commit()
        output_path = Path(cleaning_run.output_file_path)
        output_path.unlink()  # gone before the pass ever runs

        result = purge_expired_artifacts(db_session)

        assert result.purged_count == 0
        assert result.already_missing_count == 1
        events = _events_for_cleaning_run(db_session, cleaning_run.id)
        assert len(events) == 1
        assert events[0].outcome == "already_missing"
        assert events[0].dry_run is False
        assert events[0].failure_reason_code is None
        assert events[0].artifact_size_bytes is None
        db_session.refresh(cleaning_run)
        assert cleaning_run.output_deleted_at is not None
    finally:
        get_settings.cache_clear()


def test_permission_failure_records_failed_and_leaves_output_deleted_at_null(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    import os

    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        cleaning_run = _load_cleaning_run(db_session, ids["clean_run_id"])
        cleaning_run.approved_at = _ago(10)
        db_session.commit()
        output_path = Path(cleaning_run.output_file_path)
        locked_dir = output_path.parent
        os.chmod(locked_dir, 0o555)
        try:
            result = purge_expired_artifacts(db_session)
        finally:
            os.chmod(locked_dir, 0o755)  # restore so tmp_path cleanup works

        assert result.failed_count == 1
        assert result.purged_count == 0
        events = _events_for_cleaning_run(db_session, cleaning_run.id)
        assert len(events) == 1
        assert events[0].outcome == "failed"
        assert events[0].failure_reason_code == "permission_denied"
        assert events[0].completed_at is not None
        db_session.refresh(cleaning_run)
        assert cleaning_run.output_deleted_at is None
        assert output_path.exists()  # never actually removed

        # The artifact remains a candidate for a future pass once the
        # underlying problem is fixed.
        result_retry = purge_expired_artifacts(db_session)
        assert result_retry.purged_count == 1
        db_session.refresh(cleaning_run)
        assert cleaning_run.output_deleted_at is not None
        assert len(_events_for_cleaning_run(db_session, cleaning_run.id)) == 2
    finally:
        get_settings.cache_clear()


def test_unsafe_path_fails_closed(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        cleaning_run = _load_cleaning_run(db_session, ids["clean_run_id"])
        cleaning_run.approved_at = _ago(10)
        outside = tmp_path / "escaped.csv"
        outside.write_text("not a real artifact", encoding="utf-8")
        cleaning_run.output_file_path = str(outside)
        db_session.commit()

        result = purge_expired_artifacts(db_session)

        assert result.failed_count == 1
        events = _events_for_cleaning_run(db_session, cleaning_run.id)
        assert len(events) == 1
        assert events[0].outcome == "failed"
        assert events[0].failure_reason_code == "unsafe_path"
        db_session.refresh(cleaning_run)
        assert cleaning_run.output_deleted_at is None
        assert outside.exists()  # never touched
    finally:
        get_settings.cache_clear()


def test_source_input_file_is_never_touched_by_a_purge_pass(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        cleaning_run = _load_cleaning_run(db_session, ids["clean_run_id"])
        cleaning_run.approved_at = _ago(10)
        db_session.commit()

        input_file = csv_root / ids["organization_id"] / "customers.csv"
        assert input_file.exists()
        original_content = input_file.read_bytes()

        result = purge_expired_artifacts(db_session)

        assert result.purged_count == 1  # the output artifact was purged...
        assert input_file.exists()  # ...but the source file never was
        assert input_file.read_bytes() == original_content
    finally:
        get_settings.cache_clear()


# --- outcomes: dry run --------------------------------------------------------


def test_dry_run_does_not_delete_or_set_output_deleted_at(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7, dry_run=True)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        cleaning_run = _load_cleaning_run(db_session, ids["clean_run_id"])
        cleaning_run.approved_at = _ago(10)
        db_session.commit()
        output_path = Path(cleaning_run.output_file_path)
        original_size = output_path.stat().st_size

        result = purge_expired_artifacts(db_session)

        assert result.dry_run is True
        assert result.dry_run_would_purge_count == 1
        assert result.purged_count == 0
        assert output_path.exists()  # never deleted
        db_session.refresh(cleaning_run)
        assert cleaning_run.output_deleted_at is None  # never set

        events = _events_for_cleaning_run(db_session, cleaning_run.id)
        assert len(events) == 1
        assert events[0].outcome == "completed"
        assert events[0].dry_run is True
        assert events[0].artifact_size_bytes == original_size
    finally:
        get_settings.cache_clear()


def test_dry_run_against_an_already_missing_file_records_already_missing(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7, dry_run=True)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        cleaning_run = _load_cleaning_run(db_session, ids["clean_run_id"])
        cleaning_run.approved_at = _ago(10)
        db_session.commit()
        Path(cleaning_run.output_file_path).unlink()

        result = purge_expired_artifacts(db_session)

        assert result.dry_run_would_purge_count == 0
        assert result.already_missing_count == 1
        db_session.refresh(cleaning_run)
        assert cleaning_run.output_deleted_at is None

        events = _events_for_cleaning_run(db_session, cleaning_run.id)
        assert len(events) == 1
        assert events[0].outcome == "already_missing"
        assert events[0].dry_run is True
    finally:
        get_settings.cache_clear()


def test_repeated_dry_runs_reaudit_the_same_eligible_artifact(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7, dry_run=True)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        cleaning_run = _load_cleaning_run(db_session, ids["clean_run_id"])
        cleaning_run.approved_at = _ago(10)
        db_session.commit()

        first = purge_expired_artifacts(db_session)
        second = purge_expired_artifacts(db_session)

        assert first.dry_run_would_purge_count == 1
        assert second.dry_run_would_purge_count == 1
        assert Path(cleaning_run.output_file_path).exists()
        db_session.refresh(cleaning_run)
        assert cleaning_run.output_deleted_at is None
        assert len(_events_for_cleaning_run(db_session, cleaning_run.id)) == 2
    finally:
        get_settings.cache_clear()


# --- idempotency --------------------------------------------------------------


def test_second_real_pass_does_not_reprocess_a_purged_artifact(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        cleaning_run = _load_cleaning_run(db_session, ids["clean_run_id"])
        cleaning_run.approved_at = _ago(10)
        db_session.commit()

        first = purge_expired_artifacts(db_session)
        second = purge_expired_artifacts(db_session)

        assert first.purged_count == 1
        assert second.purged_count == 0
        assert second.candidates_considered == 0
        assert len(_events_for_cleaning_run(db_session, cleaning_run.id)) == 1
    finally:
        get_settings.cache_clear()


def test_one_failed_artifact_does_not_roll_back_another_completed_artifact(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    import os

    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7, batch_size=10)
    try:
        headers_a = _auth_headers(client, uuid.uuid4().hex)
        ids_a = _build_pipeline(client, db_session, csv_root, headers_a)
        run_a = _load_cleaning_run(db_session, ids_a["clean_run_id"])
        run_a.approved_at = _ago(10)

        headers_b = _auth_headers(client, uuid.uuid4().hex)
        ids_b = _build_pipeline(client, db_session, csv_root, headers_b)
        run_b = _load_cleaning_run(db_session, ids_b["clean_run_id"])
        run_b.approved_at = _ago(9)
        db_session.commit()

        locked_dir = Path(run_a.output_file_path).parent
        os.chmod(locked_dir, 0o555)
        try:
            result = purge_expired_artifacts(db_session)
        finally:
            os.chmod(locked_dir, 0o755)

        assert result.failed_count == 1
        assert result.purged_count == 1

        db_session.refresh(run_a)
        db_session.refresh(run_b)
        assert run_a.output_deleted_at is None
        assert run_b.output_deleted_at is not None
        assert Path(run_b.output_file_path).exists() is False
    finally:
        get_settings.cache_clear()


# --- multi-run-type coverage ---------------------------------------------------


def test_standardization_artifact_is_purged_using_approved_at(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        std_run = _load_standardization_run(db_session, ids["std_run_id"])
        std_run.approved_at = _ago(10)
        db_session.commit()
        output_path = Path(std_run.output_file_path)

        result = purge_expired_artifacts(db_session)

        assert result.purged_count >= 1
        db_session.refresh(std_run)
        assert std_run.output_deleted_at is not None
        assert not output_path.exists()
        events = list(
            db_session.execute(
                select(ArtifactRetentionEvent).where(
                    ArtifactRetentionEvent.standardization_run_id == std_run.id
                )
            ).scalars().all()
        )
        assert len(events) == 1
        assert events[0].outcome == "completed"
    finally:
        get_settings.cache_clear()


def test_export_artifact_is_purged_using_approved_at(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        ids = _build_pipeline(client, db_session, csv_root, headers)
        approve = client.post(
            f"/tasks/{ids['export_task_id']}/runs/{ids['export_run_id']}/export/approve",
            headers=headers,
        )
        assert approve.status_code == 200, approve.text
        export_run = _load_export_run(db_session, ids["export_run_id"])
        export_run.approved_at = _ago(10)
        db_session.commit()
        output_path = Path(export_run.output_file_path)

        result = purge_expired_artifacts(db_session)

        assert result.purged_count >= 1
        db_session.refresh(export_run)
        assert export_run.output_deleted_at is not None
        assert not output_path.exists()
        events = list(
            db_session.execute(
                select(ArtifactRetentionEvent).where(
                    ArtifactRetentionEvent.export_run_id == export_run.id
                )
            ).scalars().all()
        )
        assert len(events) == 1
        assert events[0].outcome == "completed"
    finally:
        get_settings.cache_clear()


# --- shared batch budget (production review Issue A fix) ---------------------


def test_batch_size_is_a_single_shared_budget_across_all_run_types(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """RETENTION_CLAIM_BATCH_SIZE must bound the TOTAL number of artifacts
    processed in one purge_expired_artifacts() call across cleaning,
    standardization, and export combined -- never a separate allowance
    per run type. Here 2 cleaning runs and 2 standardization runs are all
    made eligible (4 candidates total, spanning 2 run types) and
    batch_size is capped at 3: a per-run-type budget bug would let
    cleaning consume up to 2 AND standardization consume up to 2 (4
    total, exceeding the configured cap); the shared-budget
    implementation must process exactly 3 total, with cleaning (first in
    _RUN_TYPE_CONFIGS) fully draining its 2 eligible candidates before
    standardization gets whatever remains of the shared budget (1)."""
    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7, batch_size=3)
    try:
        headers_a = _auth_headers(client, uuid.uuid4().hex)
        ids_a = _build_pipeline(client, db_session, csv_root, headers_a)
        headers_b = _auth_headers(client, uuid.uuid4().hex)
        ids_b = _build_pipeline(client, db_session, csv_root, headers_b)

        cleaning_a = _load_cleaning_run(db_session, ids_a["clean_run_id"])
        cleaning_b = _load_cleaning_run(db_session, ids_b["clean_run_id"])
        std_a = _load_standardization_run(db_session, ids_a["std_run_id"])
        std_b = _load_standardization_run(db_session, ids_b["std_run_id"])
        cleaning_a.approved_at = _ago(10)
        cleaning_b.approved_at = _ago(9)
        std_a.approved_at = _ago(8)
        std_b.approved_at = _ago(7)
        db_session.commit()

        result = purge_expired_artifacts(db_session)

        # The shared budget (3) is never exceeded, even though 4
        # candidates were eligible across the two run types.
        assert result.candidates_considered == 3
        assert result.purged_count == 3

        db_session.refresh(cleaning_a)
        db_session.refresh(cleaning_b)
        db_session.refresh(std_a)
        db_session.refresh(std_b)

        # Deterministic ordering preserved: cleaning (first in
        # _RUN_TYPE_CONFIGS) fully drains its 2 eligible candidates
        # before standardization draws against what's left (1 of 2).
        assert cleaning_a.output_deleted_at is not None
        assert cleaning_b.output_deleted_at is not None
        purged_std = [r for r in (std_a, std_b) if r.output_deleted_at is not None]
        untouched_std = [r for r in (std_a, std_b) if r.output_deleted_at is None]
        assert len(purged_std) == 1
        assert len(untouched_std) == 1
        # The one standardization run actually purged is the one with
        # the earlier (more overdue) decision timestamp -- confirms the
        # remaining single unit of budget was spent deterministically,
        # not arbitrarily.
        assert purged_std[0].id == std_a.id

        # A second pass picks up exactly the one remaining eligible
        # artifact left over from the first pass's exhausted budget --
        # nothing was lost, nothing was double-counted.
        second = purge_expired_artifacts(db_session)
        assert second.purged_count == 1
        db_session.refresh(std_b)
        assert std_b.output_deleted_at is not None
    finally:
        get_settings.cache_clear()


def test_batch_size_stops_querying_later_run_types_once_budget_is_exhausted(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """A budget of exactly 1, with only a standardization candidate
    eligible in addition to 2 eligible cleaning candidates, must be
    entirely consumed by cleaning (processed first) -- standardization
    must not be touched at all in this pass."""
    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7, batch_size=1)
    try:
        headers_a = _auth_headers(client, uuid.uuid4().hex)
        ids_a = _build_pipeline(client, db_session, csv_root, headers_a)
        headers_b = _auth_headers(client, uuid.uuid4().hex)
        ids_b = _build_pipeline(client, db_session, csv_root, headers_b)

        cleaning_a = _load_cleaning_run(db_session, ids_a["clean_run_id"])
        cleaning_b = _load_cleaning_run(db_session, ids_b["clean_run_id"])
        std_a = _load_standardization_run(db_session, ids_a["std_run_id"])
        cleaning_a.approved_at = _ago(10)
        cleaning_b.approved_at = _ago(9)
        std_a.approved_at = _ago(8)
        db_session.commit()

        result = purge_expired_artifacts(db_session)

        assert result.candidates_considered == 1
        db_session.refresh(cleaning_a)
        db_session.refresh(std_a)
        assert cleaning_a.output_deleted_at is not None  # the one unit spent here
        assert std_a.output_deleted_at is None  # never reached this pass
        assert len(
            list(
                db_session.execute(
                    select(ArtifactRetentionEvent).where(
                        ArtifactRetentionEvent.standardization_run_id == std_a.id
                    )
                ).scalars().all()
            )
        ) == 0
    finally:
        get_settings.cache_clear()
