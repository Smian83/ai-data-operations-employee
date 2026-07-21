"""Module 5 CSV handler integration and idempotency tests."""
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.models.data_profile import DataProfile
from app.models.data_source import DataSource
from app.models.task import Task
from app.models.task_run import TaskRun
from app.worker.handlers.base import ExecutionContext, PermanentExecutionError
from app.worker.handlers.csv_profiling import CsvProfilingHandler


def _auth_headers(client: TestClient, suffix: str) -> dict:
    response = client.post(
        "/auth/register",
        json={
            "organization_name": f"Profile Org {suffix}",
            "email": f"profile-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Profile User",
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _make_context(
    client: TestClient,
    db_session,
    relative_path: str,
    *,
    source_type: str = "csv_upload",
) -> ExecutionContext:
    suffix = uuid.uuid4().hex
    headers = _auth_headers(client, suffix)
    source_response = client.post(
        "/data-sources",
        json={
            "name": "Uploaded Customers",
            "source_type": source_type,
            "connection_metadata": {"file_path": relative_path},
        },
        headers=headers,
    )
    assert source_response.status_code == 201, source_response.text
    task_response = client.post(
        "/tasks",
        json={
            "name": "Profile Customers",
            "task_type": "sync",
            "data_source_id": source_response.json()["id"],
        },
        headers=headers,
    )
    assert task_response.status_code == 201, task_response.text
    run_response = client.post(
        f"/tasks/{task_response.json()['id']}/runs",
        headers=headers,
    )
    assert run_response.status_code == 201, run_response.text

    task = db_session.get(Task, uuid.UUID(task_response.json()["id"]))
    run = db_session.get(TaskRun, uuid.UUID(run_response.json()["id"]))
    source = db_session.get(DataSource, uuid.UUID(source_response.json()["id"]))
    return ExecutionContext(
        task_run=run,
        task=task,
        data_source=source,
        idempotency_key=str(run.idempotency_key),
        credential_provider=None,
    )


def test_csv_handler_persists_one_profile_across_retries(
    client: TestClient,
    db_session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    csv_root = tmp_path / "csv"
    csv_root.mkdir()
    monkeypatch.setenv("CSV_INPUT_ROOT", str(csv_root))
    get_settings.cache_clear()
    try:
        context = _make_context(client, db_session, "customers.csv")
        # File lives under the calling org's own subdirectory -- see B1.
        org_dir = csv_root / str(context.data_source.organization_id)
        org_dir.mkdir(parents=True)
        (org_dir / "customers.csv").write_text(
            "id,name\n1,Ada\n2,Grace\n2,Grace\n",
            encoding="utf-8",
        )
        handler = CsvProfilingHandler()

        first = handler.execute(context)
        second = handler.execute(context)

        profiles = db_session.execute(
            select(DataProfile).where(DataProfile.task_run_id == context.task_run.id)
        ).scalars().all()
        assert len(profiles) == 1
        assert profiles[0].row_count == 3
        assert profiles[0].duplicate_row_count == 1
        assert "created" in first
        assert "already exists" in second
    finally:
        get_settings.cache_clear()


def test_csv_handler_rejects_unsupported_sync_source(
    client: TestClient,
    db_session,
) -> None:
    context = _make_context(
        client,
        db_session,
        "unused.csv",
        source_type="postgres",
    )
    with pytest.raises(PermanentExecutionError, match="not implemented"):
        CsvProfilingHandler().execute(context)


def test_csv_handler_rejects_missing_file(
    client: TestClient,
    db_session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("CSV_INPUT_ROOT", str(tmp_path))
    get_settings.cache_clear()
    try:
        context = _make_context(client, db_session, "missing.csv")
        with pytest.raises(PermanentExecutionError, match="not found"):
            CsvProfilingHandler().execute(context)
    finally:
        get_settings.cache_clear()


def test_csv_handler_reads_file_within_own_organization_directory(
    client: TestClient,
    db_session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """B1: CSV_INPUT_ROOT/{organization_id}/... is the resolved root, not the
    shared CSV_INPUT_ROOT itself -- a file placed under the calling org's own
    directory is readable."""
    csv_root = tmp_path / "csv"
    csv_root.mkdir()
    monkeypatch.setenv("CSV_INPUT_ROOT", str(csv_root))
    get_settings.cache_clear()
    try:
        context = _make_context(client, db_session, "customers.csv")
        org_dir = csv_root / str(context.data_source.organization_id)
        org_dir.mkdir(parents=True)
        (org_dir / "customers.csv").write_text("id,name\n1,Ada\n", encoding="utf-8")

        result = CsvProfilingHandler().execute(context)

        assert "csv profile created" in result
    finally:
        get_settings.cache_clear()


def test_csv_handler_cannot_read_file_belonging_to_another_organization(
    client: TestClient,
    db_session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """B1: a file that physically exists under a DIFFERENT organization's
    directory must be unreachable, even though the relative file_path is
    identical and the file is present on disk -- proves isolation is by
    organization_id, not merely by filename."""
    csv_root = tmp_path / "csv"
    csv_root.mkdir()
    monkeypatch.setenv("CSV_INPUT_ROOT", str(csv_root))
    get_settings.cache_clear()
    try:
        victim_context = _make_context(client, db_session, "shared.csv")
        victim_org_dir = csv_root / str(victim_context.data_source.organization_id)
        victim_org_dir.mkdir(parents=True)
        (victim_org_dir / "shared.csv").write_text("id,name\n1,Victim\n", encoding="utf-8")

        attacker_context = _make_context(client, db_session, "shared.csv")
        assert (
            attacker_context.data_source.organization_id
            != victim_context.data_source.organization_id
        )

        with pytest.raises(PermanentExecutionError, match="not found"):
            CsvProfilingHandler().execute(attacker_context)
    finally:
        get_settings.cache_clear()


def test_csv_handler_blocks_parent_directory_traversal_out_of_tenant_root(
    client: TestClient,
    db_session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """B1 + pre-existing traversal protection: ../ cannot be used to escape
    the calling org's own subdirectory, including to reach a sibling org's
    directory or the shared root."""
    csv_root = tmp_path / "csv"
    csv_root.mkdir()
    monkeypatch.setenv("CSV_INPUT_ROOT", str(csv_root))
    get_settings.cache_clear()
    try:
        victim_context = _make_context(client, db_session, "shared.csv")
        victim_org_dir = csv_root / str(victim_context.data_source.organization_id)
        victim_org_dir.mkdir(parents=True)
        (victim_org_dir / "shared.csv").write_text("id,name\n1,Victim\n", encoding="utf-8")

        traversal_path = f"../{victim_context.data_source.organization_id}/shared.csv"
        attacker_context = _make_context(client, db_session, traversal_path)

        with pytest.raises(PermanentExecutionError, match="escapes"):
            CsvProfilingHandler().execute(attacker_context)
    finally:
        get_settings.cache_clear()


def test_csv_handler_blocks_absolute_path_escape(
    client: TestClient,
    db_session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """B1 + pre-existing traversal protection: an absolute file_path is
    rejected outright, regardless of tenant scoping."""
    csv_root = tmp_path / "csv"
    csv_root.mkdir()
    outside = tmp_path / "outside.csv"
    outside.write_text("id,name\n1,Outside\n", encoding="utf-8")
    monkeypatch.setenv("CSV_INPUT_ROOT", str(csv_root))
    get_settings.cache_clear()
    try:
        context = _make_context(client, db_session, str(outside))

        with pytest.raises(PermanentExecutionError, match="Absolute"):
            CsvProfilingHandler().execute(context)
    finally:
        get_settings.cache_clear()
