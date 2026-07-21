"""Module 6 integration tests for CleaningHandler: full execution against a
real fixture file under the tenant-scoped CSV_INPUT_ROOT/CSV_OUTPUT_ROOT
convention, idempotency across retries (same proof pattern as Module 5's
own CsvProfilingHandler test), tenant isolation on output file placement,
and an explicit assertion that the source file's hash is unchanged after a
cleaning run -- not just a design claim."""
import hashlib
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.models.cleaning_change import CleaningChange
from app.models.cleaning_run import CleaningRun
from app.models.data_source import DataSource
from app.models.task import Task
from app.models.task_run import TaskRun
from app.worker.handlers.base import ExecutionContext, PermanentExecutionError
from app.worker.handlers.cleaning import CleaningHandler
from app.worker.handlers.csv_profiling import CsvProfilingHandler

CSV_CONTENT = (
    "id,name,amount\n"
    "1,  Ada  ,42.0\n"
    "2,Grace,3.140\n"
    "2,Grace,3.14\n"  # duplicate of row 2 once cleaned
)


def _auth_headers(client: TestClient, suffix: str) -> dict:
    response = client.post(
        "/auth/register",
        json={
            "organization_name": f"Cleaning Org {suffix}",
            "email": f"cleaning-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Cleaning User",
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _make_pipeline(
    client: TestClient,
    db_session,
    csv_root: Path,
    *,
    relative_path: str = "customers.csv",
    csv_content: str = CSV_CONTENT,
) -> tuple[ExecutionContext, ExecutionContext]:
    """Registers a fresh org, creates a CSV_UPLOAD data source, writes the
    fixture under its tenant directory, profiles it via the real Module 5
    handler (so the DataProfile matches exactly what production would
    produce), then creates a TRANSFORM task/run pointing at that SYNC run.
    Returns (sync_context, transform_context)."""
    suffix = uuid.uuid4().hex
    headers = _auth_headers(client, suffix)

    source_response = client.post(
        "/data-sources",
        json={
            "name": "Uploaded Customers",
            "source_type": "csv_upload",
            "connection_metadata": {"file_path": relative_path},
        },
        headers=headers,
    )
    assert source_response.status_code == 201, source_response.text
    source_id = source_response.json()["id"]

    sync_task_response = client.post(
        "/tasks",
        json={"name": "Sync Customers", "task_type": "sync", "data_source_id": source_id},
        headers=headers,
    )
    assert sync_task_response.status_code == 201, sync_task_response.text
    sync_task_id = sync_task_response.json()["id"]

    sync_run_response = client.post(f"/tasks/{sync_task_id}/runs", headers=headers)
    assert sync_run_response.status_code == 201, sync_run_response.text
    sync_run_id = sync_run_response.json()["id"]

    org_dir = csv_root / str(uuid.UUID(source_response.json()["organization_id"]))
    org_dir.mkdir(parents=True, exist_ok=True)
    (org_dir / relative_path).write_text(csv_content, encoding="utf-8")

    sync_task = db_session.get(Task, uuid.UUID(sync_task_id))
    sync_run = db_session.get(TaskRun, uuid.UUID(sync_run_id))
    source = db_session.get(DataSource, uuid.UUID(source_id))
    sync_context = ExecutionContext(
        task_run=sync_run,
        task=sync_task,
        data_source=source,
        idempotency_key=str(sync_run.idempotency_key),
        credential_provider=None,
    )
    CsvProfilingHandler().execute(sync_context)

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
    transform_context = ExecutionContext(
        task_run=transform_run,
        task=transform_task,
        data_source=source,
        idempotency_key=str(transform_run.idempotency_key),
        credential_provider=None,
    )
    return sync_context, transform_context


def test_cleaning_handler_persists_one_run_across_retries(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = tmp_path / "csv_in"
    csv_root.mkdir()
    output_root = tmp_path / "csv_out"
    monkeypatch.setenv("CSV_INPUT_ROOT", str(csv_root))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(output_root))
    get_settings.cache_clear()
    try:
        _, transform_context = _make_pipeline(client, db_session, csv_root)
        handler = CleaningHandler()

        first = handler.execute(transform_context)
        second = handler.execute(transform_context)

        runs = db_session.execute(
            select(CleaningRun).where(CleaningRun.task_run_id == transform_context.task_run.id)
        ).scalars().all()
        assert len(runs) == 1
        assert "cleaning run created" in first
        assert "already exists" in second
        assert runs[0].status == "pending_review"
        assert runs[0].cleaning_engine_version == "1.0"
    finally:
        get_settings.cache_clear()


def test_cleaning_handler_never_writes_to_the_source_file(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Automated proof (Acceptance Criteria item 3), not just a design
    claim: the source file's bytes are identical before and after."""
    csv_root = tmp_path / "csv_in"
    csv_root.mkdir()
    output_root = tmp_path / "csv_out"
    monkeypatch.setenv("CSV_INPUT_ROOT", str(csv_root))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(output_root))
    get_settings.cache_clear()
    try:
        _, transform_context = _make_pipeline(client, db_session, csv_root)
        source_path = (
            csv_root / str(transform_context.data_source.organization_id) / "customers.csv"
        )
        before_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()

        CleaningHandler().execute(transform_context)

        after_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        assert before_hash == after_hash
    finally:
        get_settings.cache_clear()


def test_cleaning_handler_writes_output_under_tenant_scoped_output_root(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = tmp_path / "csv_in"
    csv_root.mkdir()
    output_root = tmp_path / "csv_out"
    monkeypatch.setenv("CSV_INPUT_ROOT", str(csv_root))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(output_root))
    get_settings.cache_clear()
    try:
        _, transform_context = _make_pipeline(client, db_session, csv_root)

        CleaningHandler().execute(transform_context)

        cleaning_run = db_session.execute(
            select(CleaningRun).where(CleaningRun.task_run_id == transform_context.task_run.id)
        ).scalar_one()
        output_path = Path(cleaning_run.output_file_path)
        expected_org_dir = output_root / str(transform_context.data_source.organization_id)
        assert output_path.is_relative_to(expected_org_dir)
        assert output_path.is_file()
        # Output is never written under the input root.
        assert not output_path.is_relative_to(csv_root)
    finally:
        get_settings.cache_clear()


def test_cleaning_handler_records_bounded_changes_and_accurate_aggregate(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = tmp_path / "csv_in"
    csv_root.mkdir()
    output_root = tmp_path / "csv_out"
    monkeypatch.setenv("CSV_INPUT_ROOT", str(csv_root))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(output_root))
    get_settings.cache_clear()
    try:
        _, transform_context = _make_pipeline(client, db_session, csv_root)

        CleaningHandler().execute(transform_context)

        cleaning_run = db_session.execute(
            select(CleaningRun).where(CleaningRun.task_run_id == transform_context.task_run.id)
        ).scalar_one()
        persisted_changes = db_session.execute(
            select(CleaningChange).where(CleaningChange.cleaning_run_id == cleaning_run.id)
        ).scalars().all()

        assert cleaning_run.total_changes_count > 0
        assert len(persisted_changes) == cleaning_run.total_changes_count
        assert cleaning_run.duplicate_row_count == 1  # rows 2 and 3 clean to the same tuple
        for change in persisted_changes:
            assert change.rule_name
            assert change.reason
            assert 0.0 < change.confidence_score <= 1.0
    finally:
        get_settings.cache_clear()


def test_cleaning_handler_rejects_when_no_profile_exists_for_source_run(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = tmp_path / "csv_in"
    csv_root.mkdir()
    monkeypatch.setenv("CSV_INPUT_ROOT", str(csv_root))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(tmp_path / "csv_out"))
    get_settings.cache_clear()
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        source_response = client.post(
            "/data-sources",
            json={
                "name": "Uploaded Customers",
                "source_type": "csv_upload",
                "connection_metadata": {"file_path": "customers.csv"},
            },
            headers=headers,
        )
        source_id = source_response.json()["id"]

        sync_task_response = client.post(
            "/tasks",
            json={"name": "Sync Customers", "task_type": "sync", "data_source_id": source_id},
            headers=headers,
        )
        sync_run_response = client.post(
            f"/tasks/{sync_task_response.json()['id']}/runs", headers=headers
        )
        sync_run_id = sync_run_response.json()["id"]
        # Deliberately never run CsvProfilingHandler -- no DataProfile exists.

        transform_task_response = client.post(
            "/tasks",
            json={
                "name": "Clean Customers",
                "task_type": "transform",
                "data_source_id": source_id,
            },
            headers=headers,
        )
        transform_run_response = client.post(
            f"/tasks/{transform_task_response.json()['id']}/runs",
            json={"source_task_run_id": sync_run_id},
            headers=headers,
        )
        assert transform_run_response.status_code == 201, transform_run_response.text

        transform_task = db_session.get(Task, uuid.UUID(transform_task_response.json()["id"]))
        transform_run = db_session.get(TaskRun, uuid.UUID(transform_run_response.json()["id"]))
        source = db_session.get(DataSource, uuid.UUID(source_id))
        context = ExecutionContext(
            task_run=transform_run,
            task=transform_task,
            data_source=source,
            idempotency_key=str(transform_run.idempotency_key),
            credential_provider=None,
        )

        with pytest.raises(PermanentExecutionError, match="requires a completed profile"):
            CleaningHandler().execute(context)
    finally:
        get_settings.cache_clear()


def test_cleaning_handler_output_isolated_per_tenant_even_with_identical_relative_paths(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """B1-equivalent proof for Module 6: two different orgs cleaning a
    source at the identical relative path must never collide -- distinct
    tenant-scoped input directories, distinct tenant-scoped output
    directories, distinct CleaningRun rows."""
    csv_root = tmp_path / "csv_in"
    csv_root.mkdir()
    output_root = tmp_path / "csv_out"
    monkeypatch.setenv("CSV_INPUT_ROOT", str(csv_root))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(output_root))
    get_settings.cache_clear()
    try:
        _, context_a = _make_pipeline(
            client, db_session, csv_root, relative_path="shared.csv"
        )
        _, context_b = _make_pipeline(
            client, db_session, csv_root, relative_path="shared.csv"
        )
        assert context_a.data_source.organization_id != context_b.data_source.organization_id

        CleaningHandler().execute(context_a)
        CleaningHandler().execute(context_b)

        run_a = db_session.execute(
            select(CleaningRun).where(CleaningRun.task_run_id == context_a.task_run.id)
        ).scalar_one()
        run_b = db_session.execute(
            select(CleaningRun).where(CleaningRun.task_run_id == context_b.task_run.id)
        ).scalar_one()
        assert run_a.id != run_b.id
        assert Path(run_a.output_file_path).parent != Path(run_b.output_file_path).parent
    finally:
        get_settings.cache_clear()
