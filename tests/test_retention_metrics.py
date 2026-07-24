"""Module 13 Phase 5 tests confirming the retention Prometheus metrics
(app.worker.metrics's Module 13 block) actually move for the corresponding
purge_expired_artifacts() outcome, and do NOT move for outcomes they must
not count -- mirroring test_worker_metrics.py's own before/after-delta
convention for Counters and direct-value convention for Gauges (safe here
because tests/conftest.py's autouse _clean_tables fixture wipes every
table between tests, so each test's own enabled pass reflects only that
test's own data, not leftover rows from an earlier test).

Builds the same self-contained Sync -> Clean(approved) ->
Standardize(approved) -> Match(approved) -> Export(pending_review)
pipeline test_retention_worker.py and test_artifact_download_api.py each
build independently, per this suite's established per-file fixture
discipline."""
import os
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.models.cleaning_run import CleaningRun
from app.models.data_source import DataSource
from app.models.task import Task
from app.models.task_run import TaskRun
from app.worker import metrics
from app.worker.handlers.base import ExecutionContext
from app.worker.handlers.cleaning import CleaningHandler
from app.worker.handlers.csv_profiling import CsvProfilingHandler
from app.worker.retention import purge_expired_artifacts

CSV_CONTENT = "id,name,email\n1,jane doe,jane@example.com\n2,bob smith,bob@example.com\n"


def _auth_headers(client: TestClient, suffix: str) -> dict:
    response = client.post(
        "/auth/register",
        json={
            "organization_name": f"Retention Metrics Org {suffix}",
            "email": f"retention-metrics-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Retention Metrics Test User",
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


def _disable_retention(monkeypatch) -> None:
    monkeypatch.setenv("OUTPUT_RETENTION_ENABLED", "false")
    get_settings.cache_clear()


def _build_cleaning_run(client: TestClient, db_session, csv_root: Path, headers: dict) -> CleaningRun:
    """Sync -> Clean(auto-approved), left there -- the minimum pipeline
    needed for a single eligible/purgeable cleaning-run artifact. Standard-
    ization/match/export are irrelevant to these metrics tests."""
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

    return db_session.execute(
        select(CleaningRun).where(CleaningRun.task_run_id == uuid.UUID(clean_run_id))
    ).scalar_one()


def _ago(days: float) -> datetime:
    return datetime.now(timezone.utc) - timedelta(days=days)


def _counter_value(counter) -> float:
    return counter.collect()[0].samples[0].value


def _histogram_count(histogram) -> float:
    samples = [s for s in histogram.collect()[0].samples if s.name.endswith("_count")]
    return samples[0].value


def _gauge_value(gauge) -> float:
    return gauge.collect()[0].samples[0].value


# --- disabled pass: liveness-only metrics, nothing else -------------------


def test_disabled_pass_increments_passes_and_duration_only(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7)
    try:
        run = _build_cleaning_run(client, db_session, csv_root, _auth_headers(client, uuid.uuid4().hex))
        run.approved_at = _ago(10)
        db_session.commit()
        # One enabled pass first, to give the backlog/oldest-age gauges a
        # known, real (nonzero) value to check is left UNTOUCHED below.
        purge_expired_artifacts(db_session, dry_run=True)
        backlog_before = _gauge_value(metrics.retention_backlog_artifacts)
        oldest_age_before = _gauge_value(metrics.retention_oldest_eligible_artifact_age_seconds)
        assert backlog_before == 1.0
        assert oldest_age_before > 0.0

        _disable_retention(monkeypatch)
        passes_before = _counter_value(metrics.retention_passes_total)
        duration_count_before = _histogram_count(metrics.retention_pass_duration_seconds)
        eligible_before = _counter_value(metrics.retention_artifacts_eligible_total)
        purged_before = _counter_value(metrics.retention_artifacts_purged_total)

        result = purge_expired_artifacts(db_session)

        assert result.candidates_considered == 0
        assert _counter_value(metrics.retention_passes_total) == passes_before + 1
        assert _histogram_count(metrics.retention_pass_duration_seconds) == duration_count_before + 1
        # Nothing else moves for a disabled pass.
        assert _counter_value(metrics.retention_artifacts_eligible_total) == eligible_before
        assert _counter_value(metrics.retention_artifacts_purged_total) == purged_before
        # And the backlog/oldest-age gauges hold their last REAL value --
        # a disabled pass must not reset them to a false "empty" reading.
        assert _gauge_value(metrics.retention_backlog_artifacts) == backlog_before
        assert _gauge_value(metrics.retention_oldest_eligible_artifact_age_seconds) == oldest_age_before
    finally:
        get_settings.cache_clear()


# --- enabled pass, nothing eligible: gauges go to zero ---------------------


def test_enabled_pass_with_nothing_eligible_zeroes_the_backlog_gauges(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7)
    try:
        # Left freshly approved (not past the cutoff) -- not eligible.
        _build_cleaning_run(client, db_session, csv_root, _auth_headers(client, uuid.uuid4().hex))

        result = purge_expired_artifacts(db_session)

        assert result.candidates_considered == 0
        assert _gauge_value(metrics.retention_backlog_artifacts) == 0.0
        assert _gauge_value(metrics.retention_oldest_eligible_artifact_age_seconds) == 0.0
    finally:
        get_settings.cache_clear()


# --- real purge: purged/bytes/eligible move, backlog drains ---------------


def test_real_purge_increments_purged_bytes_and_eligible_counters(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7)
    try:
        run = _build_cleaning_run(client, db_session, csv_root, _auth_headers(client, uuid.uuid4().hex))
        run.approved_at = _ago(10)
        db_session.commit()
        artifact_size = Path(run.output_file_path).stat().st_size

        purged_before = _counter_value(metrics.retention_artifacts_purged_total)
        bytes_before = _counter_value(metrics.retention_bytes_reclaimed_total)
        eligible_before = _counter_value(metrics.retention_artifacts_eligible_total)
        dry_run_before = _counter_value(metrics.retention_dry_run_artifacts_total)
        already_missing_before = _counter_value(metrics.retention_artifacts_already_missing_total)
        failed_before = _counter_value(metrics.retention_purge_failures_total)

        result = purge_expired_artifacts(db_session)

        assert result.purged_count == 1
        assert _counter_value(metrics.retention_artifacts_purged_total) == purged_before + 1
        assert _counter_value(metrics.retention_bytes_reclaimed_total) == bytes_before + artifact_size
        assert _counter_value(metrics.retention_artifacts_eligible_total) == eligible_before + 1
        # Unrelated counters do not move.
        assert _counter_value(metrics.retention_dry_run_artifacts_total) == dry_run_before
        assert _counter_value(metrics.retention_artifacts_already_missing_total) == already_missing_before
        assert _counter_value(metrics.retention_purge_failures_total) == failed_before
        # The one eligible artifact was just purged -- nothing left in the
        # backlog.
        assert _gauge_value(metrics.retention_backlog_artifacts) == 0.0
        assert _gauge_value(metrics.retention_oldest_eligible_artifact_age_seconds) == 0.0
    finally:
        get_settings.cache_clear()


# --- dry run: dry_run counter moves, purged/bytes do not, backlog remains --


def test_dry_run_increments_dry_run_counter_not_purged_or_bytes(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7, dry_run=True)
    try:
        run = _build_cleaning_run(client, db_session, csv_root, _auth_headers(client, uuid.uuid4().hex))
        run.approved_at = _ago(10)
        db_session.commit()

        dry_run_before = _counter_value(metrics.retention_dry_run_artifacts_total)
        purged_before = _counter_value(metrics.retention_artifacts_purged_total)
        bytes_before = _counter_value(metrics.retention_bytes_reclaimed_total)

        result = purge_expired_artifacts(db_session)

        assert result.dry_run_would_purge_count == 1
        assert _counter_value(metrics.retention_dry_run_artifacts_total) == dry_run_before + 1
        assert _counter_value(metrics.retention_artifacts_purged_total) == purged_before
        assert _counter_value(metrics.retention_bytes_reclaimed_total) == bytes_before
        # Dry run never actually removes anything -- the artifact is still
        # in the backlog afterward.
        assert _gauge_value(metrics.retention_backlog_artifacts) == 1.0
        assert _gauge_value(metrics.retention_oldest_eligible_artifact_age_seconds) > 0.0
        assert Path(run.output_file_path).exists()
    finally:
        get_settings.cache_clear()


# --- failure: purge_failures_total moves, purged does not ------------------


def test_failed_purge_increments_failure_counter_not_purged(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7)
    try:
        run = _build_cleaning_run(client, db_session, csv_root, _auth_headers(client, uuid.uuid4().hex))
        run.approved_at = _ago(10)
        db_session.commit()
        locked_dir = Path(run.output_file_path).parent

        failed_before = _counter_value(metrics.retention_purge_failures_total)
        purged_before = _counter_value(metrics.retention_artifacts_purged_total)

        os.chmod(locked_dir, 0o555)
        try:
            result = purge_expired_artifacts(db_session)
        finally:
            os.chmod(locked_dir, 0o755)

        assert result.failed_count == 1
        assert _counter_value(metrics.retention_purge_failures_total) == failed_before + 1
        assert _counter_value(metrics.retention_artifacts_purged_total) == purged_before
        # A failed artifact remains in the backlog for the next pass.
        assert _gauge_value(metrics.retention_backlog_artifacts) == 1.0
    finally:
        get_settings.cache_clear()


# --- backlog reflects multiple eligible artifacts and the oldest age -------


def test_backlog_counts_multiple_artifacts_and_oldest_age_matches_the_oldest(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    _enable_retention(monkeypatch, window_days=7, dry_run=True)
    try:
        older = _build_cleaning_run(client, db_session, csv_root, _auth_headers(client, uuid.uuid4().hex))
        newer = _build_cleaning_run(client, db_session, csv_root, _auth_headers(client, uuid.uuid4().hex))
        older.approved_at = _ago(20)
        newer.approved_at = _ago(10)
        db_session.commit()

        result = purge_expired_artifacts(db_session)

        assert result.dry_run_would_purge_count == 2
        assert _gauge_value(metrics.retention_backlog_artifacts) == 2.0
        oldest_age = _gauge_value(metrics.retention_oldest_eligible_artifact_age_seconds)
        # The oldest eligible artifact is ~20 days old -- comfortably more
        # than the ~10-day-old one, with generous slack for test runtime.
        assert oldest_age > timedelta(days=19).total_seconds()
        assert oldest_age < timedelta(days=21).total_seconds()
    finally:
        get_settings.cache_clear()
