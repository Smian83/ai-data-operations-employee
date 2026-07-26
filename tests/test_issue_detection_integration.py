"""Module 14 Phase 3 end-to-end integration test: runs the real
IssueDetectionHandler against an actual CSV file (same as
test_issue_detection_handler.py), then hits the Phase 3 API endpoints
against the rows THAT HANDLER RUN actually wrote -- not manually inserted
fixture rows, unlike test_issue_detection_api.py. Proves the worker
handler and the read-only API layer agree on both the summary aggregates
and the individual Issue rows for the same real detection run."""
import uuid
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.models.data_source import DataSource
from app.models.task import Task
from app.models.task_run import TaskRun
from app.worker.handlers.base import ExecutionContext
from app.worker.handlers.issue_detection import IssueDetectionHandler


def _register(client: TestClient, suffix: str) -> dict:
    response = client.post(
        "/auth/register",
        json={
            "organization_name": f"Detect Integration Org {suffix}",
            "email": f"detect-integration-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Detect Integration User",
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _make_context(client: TestClient, db_session, headers: dict) -> ExecutionContext:
    source_response = client.post(
        "/data-sources",
        json={
            "name": "Uploaded Customers",
            "source_type": "csv_upload",
            "connection_metadata": {"file_path": "customers.csv"},
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


def test_handler_output_is_retrievable_end_to_end_through_the_api(
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
        headers = _register(client, uuid.uuid4().hex)
        context = _make_context(client, db_session, headers)
        org_dir = csv_root / str(context.data_source.organization_id)
        org_dir.mkdir(parents=True)
        (org_dir / "customers.csv").write_text(
            "id,name\n1, Ada \n2,Grace\n3,Bob  Jones\n",
            encoding="utf-8",
        )

        result = IssueDetectionHandler().execute(context)
        assert "created" in result

        task_id = str(context.task.id)
        run_id = str(context.task_run.id)

        summary_response = client.get(
            f"/tasks/{task_id}/runs/{run_id}/detection", headers=headers
        )
        assert summary_response.status_code == 200, summary_response.text
        summary = summary_response.json()
        # Three unconditional whitespace findings: leading (" Ada "),
        # trailing (" Ada "), and multiple-internal-spaces ("Bob  Jones").
        assert summary["total_issues_found"] == 3
        assert summary["persisted_issue_count"] == 3
        assert summary["rows_scanned"] == 3
        assert summary["columns_scanned"] == 2
        assert sum(summary["issues_by_severity"].values()) == 3

        issues_response = client.get(
            f"/tasks/{task_id}/runs/{run_id}/detection/issues", headers=headers
        )
        assert issues_response.status_code == 200, issues_response.text
        issues_body = issues_response.json()
        assert issues_body["total"] == 3
        assert len(issues_body["items"]) == 3
        assert all(item["dataset_id"] == str(context.data_source.id) for item in issues_body["items"])
        assert {item["issue_type"] for item in issues_body["items"]} == {
            "leading_whitespace", "trailing_whitespace", "multiple_internal_spaces"
        }

        # Filtering against real handler-produced rows.
        filtered_response = client.get(
            f"/tasks/{task_id}/runs/{run_id}/detection/issues",
            params={"issue_type": "multiple_internal_spaces"},
            headers=headers,
        )
        assert filtered_response.status_code == 200, filtered_response.text
        filtered_body = filtered_response.json()
        assert filtered_body["total"] == 1
        assert filtered_body["items"][0]["column_name"] == "name"
        assert filtered_body["items"][0]["row_number"] == 4

        # Re-running the handler (idempotent retry) must not change what
        # the API reports.
        second = IssueDetectionHandler().execute(context)
        assert "already exists" in second
        repeat_summary = client.get(
            f"/tasks/{task_id}/runs/{run_id}/detection", headers=headers
        ).json()
        assert repeat_summary["id"] == summary["id"]
        assert repeat_summary["total_issues_found"] == 3
    finally:
        get_settings.cache_clear()
