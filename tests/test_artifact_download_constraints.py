"""Module 10 tests deliberately violating every CHECK constraint added on
artifact_download_events (see database/alembic/versions/
b4c5d6e7f8a9_artifact_retrieval.py and app.models.artifact_download_event).
Runs against whatever DATABASE_URL the suite is pointed at -- SQLite in
the sandbox, real PostgreSQL during the dedicated verification pass --
since both backends enforce CHECK constraints identically (unlike FK
enforcement, which SQLite only honors once PRAGMA foreign_keys=ON, already
handled globally in app.db.session). Builds one real, fully valid
ExportRun via the same pipeline every other API test file in this suite
uses, then attempts a series of invalid ArtifactDownloadEvent inserts
against it, each expected to raise IntegrityError."""
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings
from app.models.artifact_download_event import ArtifactDownloadEvent
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
            "organization_name": f"Constraints Org {suffix}",
            "email": f"constraints-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Constraints User",
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


def _build_valid_export_run(client: TestClient, db_session, csv_root: Path) -> ExportRun:
    """Builds one real, fully valid, APPROVED ExportRun via the exact
    same pipeline test_export_api.py/test_artifact_download_api.py use,
    so every FK this test file's invalid ArtifactDownloadEvent inserts
    reference (organization_id, export_run_id) is guaranteed valid --
    isolating each assertion to the CHECK constraint under test rather
    than an incidental FK violation."""
    headers = _auth_headers(client, uuid.uuid4().hex)
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

    clean_task_id = client.post(
        "/tasks", json={"name": "Clean", "task_type": "transform", "data_source_id": source_id},
        headers=headers,
    ).json()["id"]
    clean_run_id = client.post(
        f"/tasks/{clean_task_id}/runs", json={"source_task_run_id": sync_run_id}, headers=headers
    ).json()["id"]
    clean_task = db_session.get(Task, uuid.UUID(clean_task_id))
    clean_run = db_session.get(TaskRun, uuid.UUID(clean_run_id))
    CleaningHandler().execute(
        ExecutionContext(
            task_run=clean_run, task=clean_task, data_source=source,
            idempotency_key=str(clean_run.idempotency_key), credential_provider=None,
        )
    )
    client.post(f"/tasks/{clean_task_id}/runs/{clean_run_id}/cleaning/approve", headers=headers)

    std_task_id = client.post(
        "/tasks",
        json={"name": "Standardize", "task_type": "standardize", "data_source_id": source_id},
        headers=headers,
    ).json()["id"]
    std_run_id = client.post(
        f"/tasks/{std_task_id}/runs", json={"source_task_run_id": clean_run_id}, headers=headers
    ).json()["id"]
    std_task = db_session.get(Task, uuid.UUID(std_task_id))
    std_run = db_session.get(TaskRun, uuid.UUID(std_run_id))
    StandardizationHandler().execute(
        ExecutionContext(
            task_run=std_run, task=std_task, data_source=source,
            idempotency_key=str(std_run.idempotency_key), credential_provider=None,
        )
    )
    client.post(f"/tasks/{std_task_id}/runs/{std_run_id}/standardization/approve", headers=headers)

    match_task_id = client.post(
        "/tasks", json={"name": "Match", "task_type": "match", "data_source_id": source_id},
        headers=headers,
    ).json()["id"]
    match_run_id = client.post(
        f"/tasks/{match_task_id}/runs", json={"source_task_run_id": std_run_id}, headers=headers
    ).json()["id"]
    match_task = db_session.get(Task, uuid.UUID(match_task_id))
    match_run = db_session.get(TaskRun, uuid.UUID(match_run_id))
    MatchHandler().execute(
        ExecutionContext(
            task_run=match_run, task=match_task, data_source=source,
            idempotency_key=str(match_run.idempotency_key), credential_provider=None,
        )
    )
    client.post(f"/tasks/{match_task_id}/runs/{match_run_id}/matching/approve", headers=headers)

    export_task_id = client.post(
        "/tasks", json={"name": "Export", "task_type": "export", "data_source_id": source_id},
        headers=headers,
    ).json()["id"]
    export_run_id = client.post(
        f"/tasks/{export_task_id}/runs", json={"source_task_run_id": match_run_id}, headers=headers
    ).json()["id"]
    export_task = db_session.get(Task, uuid.UUID(export_task_id))
    export_task_run = db_session.get(TaskRun, uuid.UUID(export_run_id))
    source = db_session.get(DataSource, uuid.UUID(source_id))
    ExportHandler().execute(
        ExecutionContext(
            task_run=export_task_run, task=export_task, data_source=source,
            idempotency_key=str(export_task_run.idempotency_key), credential_provider=None,
        )
    )
    client.post(f"/tasks/{export_task_id}/runs/{export_run_id}/export/approve", headers=headers)

    db_session.expire_all()
    export_run = db_session.execute(
        select(ExportRun).where(ExportRun.task_run_id == uuid.UUID(export_run_id))
    ).scalar_one()
    return export_run


@pytest.fixture
def valid_export_run(client: TestClient, db_session, tmp_path: Path, monkeypatch) -> ExportRun:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        yield _build_valid_export_run(client, db_session, csv_root)
    finally:
        get_settings.cache_clear()


def _base_kwargs(export_run: ExportRun) -> dict:
    """A minimally valid ArtifactDownloadEvent's constructor kwargs --
    each test below mutates exactly one field to an invalid value."""
    return dict(
        id=uuid.uuid4(),
        organization_id=export_run.organization_id,
        artifact_type="export",
        export_run_id=export_run.id,
        run_status_at_request="approved",
        outcome="started",
    )


def test_invalid_artifact_type_is_rejected(db_session, valid_export_run: ExportRun) -> None:
    event = ArtifactDownloadEvent(**{**_base_kwargs(valid_export_run), "artifact_type": "bogus"})
    db_session.add(event)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_zero_run_references_is_rejected(db_session, valid_export_run: ExportRun) -> None:
    kwargs = _base_kwargs(valid_export_run)
    kwargs.pop("export_run_id")
    event = ArtifactDownloadEvent(**kwargs)  # no run id set at all
    db_session.add(event)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_two_run_references_is_rejected(db_session, valid_export_run: ExportRun) -> None:
    kwargs = _base_kwargs(valid_export_run)
    # export_run_id AND cleaning_run_id both set -- ambiguous, rejected
    # even though cleaning_run_id doesn't reference a real row (the
    # exactly-one-ref CHECK is evaluated regardless of FK validity).
    kwargs["cleaning_run_id"] = uuid.uuid4()
    event = ArtifactDownloadEvent(**kwargs)
    db_session.add(event)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_invalid_run_status_at_request_is_rejected(db_session, valid_export_run: ExportRun) -> None:
    kwargs = _base_kwargs(valid_export_run)
    kwargs["run_status_at_request"] = "pending_review"  # never valid here
    event = ArtifactDownloadEvent(**kwargs)
    db_session.add(event)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_invalid_outcome_is_rejected(db_session, valid_export_run: ExportRun) -> None:
    kwargs = _base_kwargs(valid_export_run)
    kwargs["outcome"] = "bogus_outcome"
    event = ArtifactDownloadEvent(**kwargs)
    db_session.add(event)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_failure_reason_code_present_on_started_is_rejected(
    db_session, valid_export_run: ExportRun
) -> None:
    kwargs = _base_kwargs(valid_export_run)
    kwargs["outcome"] = "started"
    kwargs["failure_reason_code"] = "hash_mismatch"  # must be NULL for 'started'
    event = ArtifactDownloadEvent(**kwargs)
    db_session.add(event)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_failure_reason_code_missing_on_failure_outcome_is_rejected(
    db_session, valid_export_run: ExportRun
) -> None:
    kwargs = _base_kwargs(valid_export_run)
    kwargs["outcome"] = "integrity_failed"
    kwargs["failure_reason_code"] = None  # must be set for a failure outcome
    kwargs["completed_at"] = datetime.now(timezone.utc)
    event = ArtifactDownloadEvent(**kwargs)
    db_session.add(event)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_unrecognized_failure_reason_code_is_rejected(
    db_session, valid_export_run: ExportRun
) -> None:
    kwargs = _base_kwargs(valid_export_run)
    kwargs["outcome"] = "file_missing"
    kwargs["failure_reason_code"] = "not_a_real_code"
    kwargs["completed_at"] = datetime.now(timezone.utc)
    event = ArtifactDownloadEvent(**kwargs)
    db_session.add(event)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_completed_at_set_while_started_is_rejected(db_session, valid_export_run: ExportRun) -> None:
    kwargs = _base_kwargs(valid_export_run)
    kwargs["outcome"] = "started"
    kwargs["completed_at"] = datetime.now(timezone.utc)  # must be NULL for 'started'
    event = ArtifactDownloadEvent(**kwargs)
    db_session.add(event)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_completed_at_missing_on_terminal_outcome_is_rejected(
    db_session, valid_export_run: ExportRun
) -> None:
    kwargs = _base_kwargs(valid_export_run)
    kwargs["outcome"] = "completed"
    kwargs["completed_at"] = None  # must be set once outcome leaves 'started'
    kwargs["verified_sha256"] = valid_export_run.output_sha256
    kwargs["bytes_served"] = 10
    event = ArtifactDownloadEvent(**kwargs)
    db_session.add(event)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_negative_bytes_served_is_rejected(db_session, valid_export_run: ExportRun) -> None:
    kwargs = _base_kwargs(valid_export_run)
    kwargs["bytes_served"] = -1
    event = ArtifactDownloadEvent(**kwargs)
    db_session.add(event)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_minimally_valid_started_row_is_accepted(db_session, valid_export_run: ExportRun) -> None:
    """Sanity check that _base_kwargs() itself is valid -- otherwise
    every test above would trivially pass for the wrong reason."""
    event = ArtifactDownloadEvent(**_base_kwargs(valid_export_run))
    db_session.add(event)
    db_session.commit()  # must NOT raise
    db_session.refresh(event)
    assert event.outcome == "started"
    assert event.completed_at is None
    assert event.failure_reason_code is None
    assert event.bytes_served == 0


def test_cascade_delete_when_export_run_deleted(db_session, valid_export_run: ExportRun) -> None:
    """Section 7's ondelete=CASCADE from artifact_download_events to
    export_runs: deleting the parent ExportRun removes its events too.
    Uses a Core DELETE (not db_session.delete()) so this exercises the
    database-level ON DELETE CASCADE directly, rather than SQLAlchemy's
    ORM-level relationship cascade handling for ExportRun.exclusions
    (an unrelated, pre-existing Module 9 relationship out of scope
    here)."""
    event = ArtifactDownloadEvent(**_base_kwargs(valid_export_run))
    db_session.add(event)
    db_session.commit()
    event_id = event.id
    export_run_id = valid_export_run.id

    db_session.execute(delete(ExportRun).where(ExportRun.id == export_run_id))
    db_session.commit()

    assert db_session.get(ArtifactDownloadEvent, event_id) is None
