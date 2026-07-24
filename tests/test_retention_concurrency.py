"""Module 13: PostgreSQL-only concurrency proof for
app.worker.retention.purge_expired_artifacts -- genuine duplicate-purge
prevention can only be proven against real PostgreSQL row locking
(SELECT ... FOR UPDATE SKIP LOCKED), exactly the same precedent
tests/test_scheduled_tasks_concurrency.py already established for
run_due_schedules. Every test in this file is skipped on SQLite."""
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.artifact_retention_event import ArtifactRetentionEvent
from app.models.cleaning_run import CleaningRun
from app.models.data_source import DataSource
from app.models.task import Task
from app.models.task_run import TaskRun
from app.worker.handlers.base import ExecutionContext
from app.worker.handlers.cleaning import CleaningHandler
from app.worker.handlers.csv_profiling import CsvProfilingHandler
from app.worker.retention import purge_expired_artifacts

CSV_CONTENT = (
    "id,name,email\n"
    "1,jane doe,jane@example.com\n"
    "2,bob smith,bob@example.com\n"
)


def _require_postgresql(db_session) -> None:
    if db_session.get_bind().dialect.name != "postgresql":
        pytest.skip("Real retention concurrency verification requires PostgreSQL")


def _auth_headers(client: TestClient, suffix: str) -> dict:
    response = client.post(
        "/auth/register",
        json={
            "organization_name": f"Retention Concurrency Org {suffix}",
            "email": f"retention-concurrency-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Retention Concurrency User",
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
    monkeypatch.setenv("OUTPUT_RETENTION_ENABLED", "true")
    monkeypatch.setenv("OUTPUT_RETENTION_WINDOW_DAYS", "7")
    monkeypatch.setenv("OUTPUT_RETENTION_DRY_RUN", "false")
    monkeypatch.setenv("RETENTION_CLAIM_BATCH_SIZE", "10")
    get_settings.cache_clear()
    return csv_root


def _build_eligible_cleaning_run(client: TestClient, db_session, csv_root: Path, suffix: str) -> CleaningRun:
    headers = _auth_headers(client, suffix)
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
    approve = client.post(f"/tasks/{clean_task_id}/runs/{clean_run_id}/cleaning/approve", headers=headers)
    assert approve.status_code == 200, approve.text

    cleaning_run = db_session.execute(
        select(CleaningRun).where(CleaningRun.task_run_id == uuid.UUID(clean_run_id))
    ).scalar_one()
    cleaning_run.approved_at = datetime.now(timezone.utc) - timedelta(days=10)
    db_session.commit()
    return cleaning_run


def test_two_concurrent_retention_workers_purge_one_artifact_exactly_once(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    _require_postgresql(db_session)
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        cleaning_run = _build_eligible_cleaning_run(client, db_session, csv_root, uuid.uuid4().hex)
        run_id = cleaning_run.id
        output_path = Path(cleaning_run.output_file_path)
        assert output_path.exists()

        barrier = threading.Barrier(2)
        results = []
        errors: list[Exception] = []

        def _worker(worker_label: str) -> None:
            session = SessionLocal()
            try:
                barrier.wait(timeout=5)
                result = purge_expired_artifacts(session, batch_size=10)
                results.append(result)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                session.close()

        t1 = threading.Thread(target=_worker, args=("retention-1",))
        t2 = threading.Thread(target=_worker, args=("retention-2",))
        t1.start()
        t2.start()
        t1.join(timeout=15)
        t2.join(timeout=15)

        assert not errors, f"worker threads raised: {errors}"
        # Exactly one of the two concurrent passes actually purged the
        # artifact; the other found zero candidates (SKIP LOCKED skipped
        # the already-locked row, or the guarded UPDATE's rowcount guard
        # caught the race and excluded it for the rest of its own pass).
        purged_totals = sorted(r.purged_count for r in results)
        assert purged_totals == [0, 1]

        assert not output_path.exists()
        db_session.expire_all()
        refreshed = db_session.get(CleaningRun, run_id)
        assert refreshed.output_deleted_at is not None

        events = list(
            db_session.execute(
                select(ArtifactRetentionEvent).where(
                    ArtifactRetentionEvent.cleaning_run_id == run_id
                )
            ).scalars().all()
        )
        assert len(events) == 1
        assert events[0].outcome == "completed"
    finally:
        get_settings.cache_clear()


def test_two_concurrent_retention_workers_across_multiple_artifacts_purge_each_exactly_once(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    _require_postgresql(db_session)
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        run_ids = []
        for i in range(6):
            cleaning_run = _build_eligible_cleaning_run(client, db_session, csv_root, f"multi-{i}-{uuid.uuid4().hex}")
            run_ids.append(cleaning_run.id)

        barrier = threading.Barrier(2)
        results = []
        errors: list[Exception] = []

        def _worker(worker_label: str) -> None:
            session = SessionLocal()
            try:
                barrier.wait(timeout=5)
                result = purge_expired_artifacts(session, batch_size=10)
                results.append(result)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                session.close()

        threads = [threading.Thread(target=_worker, args=(f"retention-{i}",)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)

        assert not errors, f"worker threads raised: {errors}"
        assert sum(r.purged_count for r in results) == 6

        db_session.expire_all()
        for run_id in run_ids:
            refreshed = db_session.get(CleaningRun, run_id)
            assert refreshed.output_deleted_at is not None
            events = list(
                db_session.execute(
                    select(ArtifactRetentionEvent).where(
                        ArtifactRetentionEvent.cleaning_run_id == run_id
                    )
                ).scalars().all()
            )
            assert len(events) == 1, f"run {run_id} got {len(events)} retention events, expected exactly 1"
            assert events[0].outcome == "completed"
    finally:
        get_settings.cache_clear()
