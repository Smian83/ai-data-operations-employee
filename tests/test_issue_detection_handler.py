"""Module 14 Phase 2 IssueDetectionHandler integration and idempotency
tests. Mirrors test_csv_profiling_handler.py's fixture/isolation-test
conventions exactly, since IssueDetectionHandler reads the raw source CSV
the same way CsvProfilingHandler does."""
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.models.data_source import DataSource
from app.models.issue import Issue
from app.models.issue_detection_column_rule import IssueDetectionColumnRule
from app.models.issue_detection_run import IssueDetectionRun
from app.models.task import Task
from app.models.task_run import TaskRun
from app.worker.handlers.base import ExecutionContext, PermanentExecutionError
from app.worker.handlers.issue_detection import IssueDetectionHandler


def _auth_headers(client: TestClient, suffix: str) -> dict:
    response = client.post(
        "/auth/register",
        json={
            "organization_name": f"Detect Org {suffix}",
            "email": f"detect-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Detect User",
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
            "name": "Detect Issues",
            "task_type": "detect",
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


def _write_source_csv(csv_root: Path, context: ExecutionContext, text: str) -> None:
    org_dir = csv_root / str(context.data_source.organization_id)
    org_dir.mkdir(parents=True, exist_ok=True)
    (org_dir / "customers.csv").write_text(text, encoding="utf-8")


def test_handler_persists_one_detection_run_across_retries(
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
        _write_source_csv(
            csv_root,
            context,
            "id,email,name\n1,good@example.com,Ada\n2,bad-email, Grace\n",
        )
        handler = IssueDetectionHandler()

        first = handler.execute(context)
        second = handler.execute(context)

        runs = db_session.execute(
            select(IssueDetectionRun).where(
                IssueDetectionRun.task_run_id == context.task_run.id
            )
        ).scalars().all()
        assert len(runs) == 1
        # Unconfigured columns -- only the unconditional whitespace rule
        # fires (" Grace" has leading whitespace); email format checking
        # requires an explicit IssueDetectionColumnRule, which none exists
        # for this run.
        assert runs[0].total_issues_found == 1
        assert runs[0].persisted_issue_count == 1
        assert set(runs[0].issues_by_severity) == {"INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"}
        assert "created" in first
        assert "already exists" in second
    finally:
        get_settings.cache_clear()


def test_handler_applies_configured_column_rules(
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
        _write_source_csv(
            csv_root,
            context,
            "id,email\n1,good@example.com\n2,not-an-email\n",
        )
        db_session.add(
            IssueDetectionColumnRule(
                organization_id=context.data_source.organization_id,
                data_source_id=None,  # org-wide rule
                column_name="email",
                expected_type="email",
                is_required=True,
            )
        )
        db_session.commit()

        result = IssueDetectionHandler().execute(context)
        assert "created" in result

        detection_run = db_session.execute(
            select(IssueDetectionRun).where(
                IssueDetectionRun.task_run_id == context.task_run.id
            )
        ).scalar_one()
        issues = db_session.execute(
            select(Issue).where(Issue.detection_run_id == detection_run.id)
        ).scalars().all()
        issue_types = {issue.issue_type for issue in issues}
        assert "invalid_email" in issue_types
    finally:
        get_settings.cache_clear()


def test_handler_data_source_specific_rule_overrides_org_wide_rule(
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
        _write_source_csv(csv_root, context, "id,status\n1,active\n2,bogus\n")

        # Org-wide rule has no allowed_values restriction at all.
        db_session.add(
            IssueDetectionColumnRule(
                organization_id=context.data_source.organization_id,
                data_source_id=None,
                column_name="status",
            )
        )
        # Data-source-specific rule DOES restrict allowed values -- must win.
        db_session.add(
            IssueDetectionColumnRule(
                organization_id=context.data_source.organization_id,
                data_source_id=context.data_source.id,
                column_name="status",
                allowed_values=["active", "inactive"],
            )
        )
        db_session.commit()

        IssueDetectionHandler().execute(context)

        detection_run = db_session.execute(
            select(IssueDetectionRun).where(
                IssueDetectionRun.task_run_id == context.task_run.id
            )
        ).scalar_one()
        issues = db_session.execute(
            select(Issue).where(Issue.detection_run_id == detection_run.id)
        ).scalars().all()
        assert any(issue.issue_type == "invalid_enum_value" for issue in issues)
    finally:
        get_settings.cache_clear()


def test_handler_rejects_unsupported_source_type(
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
        IssueDetectionHandler().execute(context)


def test_handler_rejects_missing_file(
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
            IssueDetectionHandler().execute(context)
    finally:
        get_settings.cache_clear()


def test_handler_never_writes_to_the_source_file(
    client: TestClient,
    db_session,
    tmp_path: Path,
    monkeypatch,
) -> None:
    """Approved correction #8: source files must remain completely
    untouched. Verified by hashing the file before/after execution."""
    import hashlib

    csv_root = tmp_path / "csv"
    csv_root.mkdir()
    monkeypatch.setenv("CSV_INPUT_ROOT", str(csv_root))
    get_settings.cache_clear()
    try:
        context = _make_context(client, db_session, "customers.csv")
        _write_source_csv(csv_root, context, "id,name\n1, Ada \n2,Grace\n")
        source_path = csv_root / str(context.data_source.organization_id) / "customers.csv"
        before = hashlib.sha256(source_path.read_bytes()).hexdigest()

        IssueDetectionHandler().execute(context)

        after = hashlib.sha256(source_path.read_bytes()).hexdigest()
        assert before == after
    finally:
        get_settings.cache_clear()
