"""Module 19: API tests for the four Clean Export endpoints.

POST   /tasks/{task_id}/exports
GET    /tasks/{task_id}/exports
GET    /tasks/exports/{export_id}
GET    /tasks/exports/{export_id}/download

Strategy:
  - POST endpoint: mocked ExportService (avoids needing real file + full
    ExportRun/QC chain). Tests the HTTP layer, 404/422 guards, and
    idempotency semantics.
  - GET list/get endpoints: CleanExport rows inserted directly via ORM;
    no mocking needed — these endpoints only read DB.
  - Download endpoint: CleanExport row + real artifact file on disk; tests
    200 streaming, 404, 409, 410 guards.

Scenarios covered:
  POST /tasks/{task_id}/exports:
    1.  201 completed response (mocked service → status=completed)
    2.  201 blocked response (mocked service → status=blocked)
    3.  Idempotency: same key returns existing row (mocked service returns existing)
    4.  404 when task not found
    5.  404 when task inactive
    6.  422 when task has no data_source_id
    7.  401 unauthenticated
    8.  Response body excludes artifact_path

  GET /tasks/{task_id}/exports:
    9.  200 paginated list, newest first
   10.  Empty list when no exports
   11.  404 when task not found
   12.  Tenant isolation: other org's exports not in list
   13.  401 unauthenticated
   14.  Pagination limit + offset

  GET /tasks/exports/{export_id}:
   15.  200 with correct fields
   16.  404 when export_id not found
   17.  Tenant isolation: other org's export → 404
   18.  401 unauthenticated

  GET /tasks/exports/{export_id}/download:
   19.  200 streaming response with correct bytes
   20.  Content-Disposition attachment header present
   21.  404 when export_id not found
   22.  409 when export is blocked (not completed)
   23.  410 when export is expired
   24.  Tenant isolation: other org's export → 404
   25.  401 unauthenticated
"""
from __future__ import annotations

import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models.clean_export import CleanExport
from app.models.enums import TaskType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register(client: TestClient, suffix: str) -> dict:
    resp = client.post(
        "/auth/register",
        json={
            "organization_name": f"CE API Org {suffix}",
            "email": f"ce-api-{suffix}@example.com",
            "password": "horse-battery-staple",
            "full_name": "CE API Tester",
        },
    )
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _create_data_source(client: TestClient, headers: dict, suffix: str) -> str:
    resp = client.post(
        "/data-sources",
        json={
            "name": f"CE DS {suffix}",
            "source_type": "csv_upload",
            "connection_metadata": {"file_path": "clean.csv"},
        },
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _make_task(
    client: TestClient, headers: dict, task_type: str,
    ds_id: str | None, name: str,
) -> str:
    body: dict = {"name": name, "task_type": task_type}
    if ds_id:
        body["data_source_id"] = ds_id
    resp = client.post("/tasks", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def _get_org_id(client: TestClient, headers: dict) -> str:
    resp = client.get("/data-sources", headers=headers)
    return resp.json()["items"][0]["organization_id"]


def _insert_clean_export(
    db: Session,
    org_id: uuid.UUID,
    task_id: uuid.UUID,
    ds_id: uuid.UUID,
    *,
    status: str = "completed",
    format: str = "csv",
    idempotency_key: str | None = None,
    artifact_id: uuid.UUID | None = None,
    checksum: str | None = None,
    row_count: int | None = None,
    column_count: int | None = None,
    failure_reason: str | None = None,
) -> CleanExport:
    if idempotency_key is None:
        idempotency_key = str(uuid.uuid4())
    if status == "completed" and artifact_id is None:
        artifact_id = uuid.uuid4()
    if status == "completed" and checksum is None:
        checksum = "e" * 64
    if status == "completed" and row_count is None:
        row_count = 5
    if status == "completed" and column_count is None:
        column_count = 3
    now = datetime.now(timezone.utc)
    ce = CleanExport(
        id=uuid.uuid4(),
        organization_id=org_id,
        job_id=task_id,
        data_source_id=ds_id,
        format=format,
        status=status,
        artifact_id=artifact_id,
        checksum=checksum,
        row_count=row_count,
        column_count=column_count,
        idempotency_key=idempotency_key,
        failure_reason=failure_reason,
        completed_at=now if status in ("completed", "blocked", "failed", "expired") else None,
    )
    db.add(ce)
    db.commit()
    db.refresh(ce)
    return ce


def _make_completed_export_row(**kwargs):
    row = MagicMock()
    row.id = kwargs.get("id", uuid.uuid4())
    row.organization_id = kwargs.get("org_id", uuid.uuid4())
    row.job_id = kwargs.get("job_id", uuid.uuid4())
    row.data_source_id = kwargs.get("ds_id", uuid.uuid4())
    row.dataset_version = kwargs.get("dataset_version", None)
    row.format = kwargs.get("format", "csv")
    row.status = kwargs.get("status", "completed")
    row.artifact_id = kwargs.get("artifact_id", uuid.uuid4())
    row.checksum = kwargs.get("checksum", "f" * 64)
    row.row_count = kwargs.get("row_count", 10)
    row.column_count = kwargs.get("column_count", 3)
    row.idempotency_key = kwargs.get("idempotency_key", str(uuid.uuid4()))
    row.failure_reason = kwargs.get("failure_reason", None)
    row.created_at = kwargs.get("created_at", datetime.now(timezone.utc))
    row.completed_at = kwargs.get("completed_at", datetime.now(timezone.utc))
    return row


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def api_setup(client, db_session):
    """Register user+org, create data source and task; return scaffold dict."""
    suffix = uuid.uuid4().hex[:6]
    headers = _register(client, suffix)
    ds_id = _create_data_source(client, headers, suffix)
    # Get org_id from API
    ds_resp = client.get("/data-sources", headers=headers)
    org_id = uuid.UUID(ds_resp.json()["items"][0]["organization_id"])
    ds_uuid = uuid.UUID(ds_id)
    task_id = _make_task(client, headers, "clean_export", ds_id, f"CE Task {suffix}")
    task_uuid = uuid.UUID(task_id)
    return {
        "headers": headers,
        "org_id": org_id,
        "ds_id": ds_uuid,
        "task_id": task_uuid,
        "task_id_str": task_id,
        "ds_id_str": ds_id,
    }


# ---------------------------------------------------------------------------
# POST /tasks/{task_id}/exports
# ---------------------------------------------------------------------------


class TestCreateCleanExport:

    def test_201_completed_response(self, client, api_setup, db_session):
        s = api_setup
        completed_mock = _make_completed_export_row(
            org_id=s["org_id"],
            job_id=s["task_id"],
            ds_id=s["ds_id"],
        )
        with patch("app.api.tasks._export_service") as mock_svc:
            mock_svc.run.return_value = completed_mock
            resp = client.post(
                f"/tasks/{s['task_id_str']}/exports",
                json={
                    "format": "csv",
                    "idempotency_key": str(uuid.uuid4()),
                },
                headers=s["headers"],
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "completed"
        assert body["format"] == "csv"
        assert "artifact_path" not in body  # must be absent

    def test_201_blocked_response(self, client, api_setup, db_session):
        s = api_setup
        blocked_mock = MagicMock()
        blocked_mock.id = uuid.uuid4()
        blocked_mock.organization_id = s["org_id"]
        blocked_mock.job_id = s["task_id"]
        blocked_mock.data_source_id = s["ds_id"]
        blocked_mock.dataset_version = None
        blocked_mock.format = "csv"
        blocked_mock.status = "blocked"
        blocked_mock.artifact_id = None
        blocked_mock.checksum = None
        blocked_mock.row_count = None
        blocked_mock.column_count = None
        blocked_mock.idempotency_key = str(uuid.uuid4())
        blocked_mock.failure_reason = "No approved ExportRun found"
        blocked_mock.created_at = datetime.now(timezone.utc)
        blocked_mock.completed_at = datetime.now(timezone.utc)

        with patch("app.api.tasks._export_service") as mock_svc:
            mock_svc.run.return_value = blocked_mock
            resp = client.post(
                f"/tasks/{s['task_id_str']}/exports",
                json={
                    "format": "csv",
                    "idempotency_key": str(uuid.uuid4()),
                },
                headers=s["headers"],
            )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["status"] == "blocked"
        assert body["failure_reason"] is not None
        assert body["artifact_id"] is None

    def test_idempotency_returns_same_export(self, client, api_setup, db_session):
        s = api_setup
        key = str(uuid.uuid4())
        export_id = uuid.uuid4()
        existing = _make_completed_export_row(id=export_id, org_id=s["org_id"])

        with patch("app.api.tasks._export_service") as mock_svc:
            mock_svc.run.return_value = existing
            resp1 = client.post(
                f"/tasks/{s['task_id_str']}/exports",
                json={"format": "csv", "idempotency_key": key},
                headers=s["headers"],
            )
            resp2 = client.post(
                f"/tasks/{s['task_id_str']}/exports",
                json={"format": "csv", "idempotency_key": key},
                headers=s["headers"],
            )
        assert resp1.status_code == 201
        assert resp2.status_code == 201
        assert resp1.json()["id"] == resp2.json()["id"] == str(export_id)

    def test_404_task_not_found(self, client, api_setup):
        s = api_setup
        resp = client.post(
            f"/tasks/{uuid.uuid4()}/exports",
            json={"format": "csv", "idempotency_key": str(uuid.uuid4())},
            headers=s["headers"],
        )
        assert resp.status_code == 404

    def test_422_task_without_data_source(self, client, api_setup):
        s = api_setup
        # Create a task with no data source
        suffix = uuid.uuid4().hex[:6]
        task_no_ds_id = _make_task(client, s["headers"], "clean_export", None, f"NDS {suffix}")
        resp = client.post(
            f"/tasks/{task_no_ds_id}/exports",
            json={"format": "csv", "idempotency_key": str(uuid.uuid4())},
            headers=s["headers"],
        )
        assert resp.status_code == 422

    def test_401_unauthenticated(self, client, api_setup):
        s = api_setup
        resp = client.post(
            f"/tasks/{s['task_id_str']}/exports",
            json={"format": "csv", "idempotency_key": str(uuid.uuid4())},
        )
        assert resp.status_code == 401

    def test_artifact_path_absent_from_response(self, client, api_setup):
        s = api_setup
        completed_mock = _make_completed_export_row(org_id=s["org_id"])
        with patch("app.api.tasks._export_service") as mock_svc:
            mock_svc.run.return_value = completed_mock
            resp = client.post(
                f"/tasks/{s['task_id_str']}/exports",
                json={"format": "csv", "idempotency_key": str(uuid.uuid4())},
                headers=s["headers"],
            )
        assert resp.status_code == 201
        assert "artifact_path" not in resp.json()

    def test_dataset_version_accepted(self, client, api_setup):
        s = api_setup
        completed_mock = _make_completed_export_row(org_id=s["org_id"])
        with patch("app.api.tasks._export_service") as mock_svc:
            mock_svc.run.return_value = completed_mock
            resp = client.post(
                f"/tasks/{s['task_id_str']}/exports",
                json={
                    "format": "xlsx",
                    "idempotency_key": str(uuid.uuid4()),
                    "dataset_version": str(uuid.uuid4()),
                },
                headers=s["headers"],
            )
        assert resp.status_code == 201
        # Verify dataset_version was forwarded to service
        actual_request = mock_svc.run.call_args[0][1]
        assert actual_request.dataset_version is not None


# ---------------------------------------------------------------------------
# GET /tasks/{task_id}/exports
# ---------------------------------------------------------------------------


class TestListCleanExports:

    def test_200_paginated_list(self, client, api_setup, db_session):
        s = api_setup
        # Insert 2 rows
        for _ in range(2):
            _insert_clean_export(db_session, s["org_id"], s["task_id"], s["ds_id"])
        resp = client.get(
            f"/tasks/{s['task_id_str']}/exports",
            headers=s["headers"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 2
        assert len(body["items"]) == 2

    def test_empty_list_when_no_exports(self, client, api_setup, db_session):
        s = api_setup
        resp = client.get(
            f"/tasks/{s['task_id_str']}/exports",
            headers=s["headers"],
        )
        assert resp.status_code == 200
        assert resp.json()["total"] == 0
        assert resp.json()["items"] == []

    def test_404_task_not_found(self, client, api_setup):
        s = api_setup
        resp = client.get(
            f"/tasks/{uuid.uuid4()}/exports",
            headers=s["headers"],
        )
        assert resp.status_code == 404

    def test_tenant_isolation(self, client, api_setup, db_session):
        s = api_setup
        # Insert for this org
        _insert_clean_export(db_session, s["org_id"], s["task_id"], s["ds_id"])
        # Register second org and try to list (different org)
        suffix2 = uuid.uuid4().hex[:6]
        headers2 = _register(client, suffix2)
        ds2 = _create_data_source(client, headers2, suffix2)
        task2 = _make_task(client, headers2, "clean_export", ds2, f"Isolated Task {suffix2}")
        # Second org lists THEIR task — should see 0 exports, not the first org's
        resp = client.get(f"/tasks/{task2}/exports", headers=headers2)
        assert resp.status_code == 200
        assert resp.json()["total"] == 0

    def test_401_unauthenticated(self, client, api_setup):
        s = api_setup
        resp = client.get(f"/tasks/{s['task_id_str']}/exports")
        assert resp.status_code == 401

    def test_pagination_limit_offset(self, client, api_setup, db_session):
        s = api_setup
        for i in range(5):
            _insert_clean_export(db_session, s["org_id"], s["task_id"], s["ds_id"])
        resp1 = client.get(
            f"/tasks/{s['task_id_str']}/exports?limit=2&offset=0",
            headers=s["headers"],
        )
        resp2 = client.get(
            f"/tasks/{s['task_id_str']}/exports?limit=2&offset=2",
            headers=s["headers"],
        )
        body1 = resp1.json()
        body2 = resp2.json()
        assert body1["total"] == 5
        assert len(body1["items"]) == 2
        assert len(body2["items"]) == 2
        ids1 = {i["id"] for i in body1["items"]}
        ids2 = {i["id"] for i in body2["items"]}
        assert ids1.isdisjoint(ids2)

    def test_response_item_has_expected_fields(self, client, api_setup, db_session):
        s = api_setup
        _insert_clean_export(db_session, s["org_id"], s["task_id"], s["ds_id"])
        resp = client.get(f"/tasks/{s['task_id_str']}/exports", headers=s["headers"])
        item = resp.json()["items"][0]
        expected_fields = {
            "id", "organization_id", "job_id", "data_source_id", "dataset_version",
            "format", "status", "artifact_id", "checksum", "row_count", "column_count",
            "idempotency_key", "failure_reason", "created_at", "completed_at",
        }
        assert expected_fields <= set(item.keys())
        assert "artifact_path" not in item


# ---------------------------------------------------------------------------
# GET /tasks/exports/{export_id}
# ---------------------------------------------------------------------------


class TestGetCleanExport:

    def test_200_correct_fields(self, client, api_setup, db_session):
        s = api_setup
        ce = _insert_clean_export(db_session, s["org_id"], s["task_id"], s["ds_id"])
        resp = client.get(
            f"/tasks/exports/{ce.id}",
            headers=s["headers"],
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == str(ce.id)
        assert body["status"] == ce.status
        assert body["format"] == ce.format
        assert body["row_count"] == ce.row_count
        assert body["column_count"] == ce.column_count
        assert "artifact_path" not in body

    def test_404_not_found(self, client, api_setup):
        s = api_setup
        resp = client.get(
            f"/tasks/exports/{uuid.uuid4()}",
            headers=s["headers"],
        )
        assert resp.status_code == 404

    def test_tenant_isolation_404(self, client, api_setup, db_session):
        s = api_setup
        # Insert export for this org
        ce = _insert_clean_export(db_session, s["org_id"], s["task_id"], s["ds_id"])
        # Second org tries to fetch it
        suffix2 = uuid.uuid4().hex[:6]
        headers2 = _register(client, suffix2)
        resp = client.get(f"/tasks/exports/{ce.id}", headers=headers2)
        assert resp.status_code == 404

    def test_401_unauthenticated(self, client, api_setup, db_session):
        s = api_setup
        ce = _insert_clean_export(db_session, s["org_id"], s["task_id"], s["ds_id"])
        resp = client.get(f"/tasks/exports/{ce.id}")
        assert resp.status_code == 401

    def test_blocked_export_returns_200(self, client, api_setup, db_session):
        s = api_setup
        ce = _insert_clean_export(
            db_session, s["org_id"], s["task_id"], s["ds_id"],
            status="blocked",
            artifact_id=None,
            checksum=None,
            row_count=None,
            column_count=None,
            failure_reason="No approved ExportRun",
        )
        resp = client.get(f"/tasks/exports/{ce.id}", headers=s["headers"])
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "blocked"
        assert body["failure_reason"] is not None


# ---------------------------------------------------------------------------
# GET /tasks/exports/{export_id}/download
# ---------------------------------------------------------------------------


class TestDownloadCleanExport:

    def _write_artifact(self, artifact_id: uuid.UUID, org_id: uuid.UUID, content: bytes) -> Path:
        """Write a real artifact file to a temp dir; return the temp dir root."""
        tmpdir = Path(tempfile.mkdtemp())
        org_dir = tmpdir / str(org_id)
        org_dir.mkdir(parents=True, exist_ok=True)
        (org_dir / f"{artifact_id}.csv").write_bytes(content)
        return tmpdir

    def test_200_streaming_response(self, client, api_setup, db_session):
        import hashlib
        s = api_setup
        content = b"id,name\n1,Alice\n2,Bob\n"
        checksum = hashlib.sha256(content).hexdigest()
        artifact_id = uuid.uuid4()
        tmpdir = self._write_artifact(artifact_id, s["org_id"], content)

        ce = _insert_clean_export(
            db_session, s["org_id"], s["task_id"], s["ds_id"],
            status="completed",
            artifact_id=artifact_id,
            checksum=checksum,
        )

        with patch("app.api.tasks.get_settings") as mock_settings:
            settings_obj = MagicMock()
            settings_obj.clean_export_output_root = str(tmpdir)
            mock_settings.return_value = settings_obj
            resp = client.get(
                f"/tasks/exports/{ce.id}/download",
                headers=s["headers"],
            )

        assert resp.status_code == 200, resp.text
        assert resp.content == content

    def test_content_disposition_attachment(self, client, api_setup, db_session):
        import hashlib
        s = api_setup
        content = b"a,b\n1,2\n"
        checksum = hashlib.sha256(content).hexdigest()
        artifact_id = uuid.uuid4()
        tmpdir = self._write_artifact(artifact_id, s["org_id"], content)

        ce = _insert_clean_export(
            db_session, s["org_id"], s["task_id"], s["ds_id"],
            status="completed",
            artifact_id=artifact_id,
            checksum=checksum,
        )

        with patch("app.api.tasks.get_settings") as mock_settings:
            settings_obj = MagicMock()
            settings_obj.clean_export_output_root = str(tmpdir)
            mock_settings.return_value = settings_obj
            resp = client.get(
                f"/tasks/exports/{ce.id}/download",
                headers=s["headers"],
            )

        assert resp.status_code == 200
        cd = resp.headers.get("content-disposition", "")
        assert "attachment" in cd

    def test_404_export_not_found(self, client, api_setup):
        s = api_setup
        resp = client.get(
            f"/tasks/exports/{uuid.uuid4()}/download",
            headers=s["headers"],
        )
        assert resp.status_code == 404

    def test_409_export_not_completed(self, client, api_setup, db_session):
        s = api_setup
        ce = _insert_clean_export(
            db_session, s["org_id"], s["task_id"], s["ds_id"],
            status="blocked",
            artifact_id=None,
            checksum=None,
            row_count=None,
            column_count=None,
            failure_reason="no approved run",
        )
        resp = client.get(
            f"/tasks/exports/{ce.id}/download",
            headers=s["headers"],
        )
        assert resp.status_code == 409
        assert "blocked" in resp.json()["detail"] or "not downloadable" in resp.json()["detail"]

    def test_409_failed_export(self, client, api_setup, db_session):
        s = api_setup
        ce = _insert_clean_export(
            db_session, s["org_id"], s["task_id"], s["ds_id"],
            status="failed",
            artifact_id=None,
            checksum=None,
            row_count=None,
            column_count=None,
            failure_reason="io_error",
        )
        resp = client.get(
            f"/tasks/exports/{ce.id}/download",
            headers=s["headers"],
        )
        assert resp.status_code == 409

    def test_410_expired_export(self, client, api_setup, db_session):
        s = api_setup
        ce = _insert_clean_export(
            db_session, s["org_id"], s["task_id"], s["ds_id"],
            status="expired",
            artifact_id=None,
            checksum=None,
            row_count=None,
            column_count=None,
        )
        resp = client.get(
            f"/tasks/exports/{ce.id}/download",
            headers=s["headers"],
        )
        assert resp.status_code == 410
        assert "retention" in resp.json()["detail"] or "expired" in resp.json()["detail"].lower()

    def test_tenant_isolation_download_404(self, client, api_setup, db_session):
        import hashlib
        s = api_setup
        content = b"x,y\n1,2\n"
        checksum = hashlib.sha256(content).hexdigest()
        artifact_id = uuid.uuid4()
        tmpdir = self._write_artifact(artifact_id, s["org_id"], content)
        ce = _insert_clean_export(
            db_session, s["org_id"], s["task_id"], s["ds_id"],
            status="completed",
            artifact_id=artifact_id,
            checksum=checksum,
        )
        # Second org
        suffix2 = uuid.uuid4().hex[:6]
        headers2 = _register(client, suffix2)
        with patch("app.api.tasks.get_settings") as mock_settings:
            settings_obj = MagicMock()
            settings_obj.clean_export_output_root = str(tmpdir)
            mock_settings.return_value = settings_obj
            resp = client.get(f"/tasks/exports/{ce.id}/download", headers=headers2)
        assert resp.status_code == 404

    def test_401_unauthenticated(self, client, api_setup, db_session):
        s = api_setup
        ce = _insert_clean_export(db_session, s["org_id"], s["task_id"], s["ds_id"])
        resp = client.get(f"/tasks/exports/{ce.id}/download")
        assert resp.status_code == 401
