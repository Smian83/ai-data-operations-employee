"""Module 20: model, schema, migration, and engine purity tests.

Scenarios covered (18):
  live schema audit (all columns present on report_runs table):
    1.  id column present
    2.  organization_id column present
    3.  task_run_id column present
    4.  task_id column present
    5.  data_source_id column present
    6.  quality_control_run_id column present (nullable)
    7.  report_engine_version column present
    8.  report_schema_version column present
    9.  report_data column present
   10.  created_at column present
  nullable checks:
   11.  quality_control_run_id is nullable; row inserts with None
   12.  report_data is NOT NULL; blank dict still valid
  unique constraint enforcement:
   13.  uq_report_runs_task_run_id: second insert with same task_run_id raises
   14.  uq_report_runs_org_id: second insert with same (org, id) raises
  identifier length audit:
   15.  table name <= 63 characters
   16.  all column names <= 63 characters
   17.  all constraint names <= 63 characters
  engine purity:
   18.  app/reports/ package has no SQLAlchemy / FastAPI / worker imports
"""
from __future__ import annotations

import ast
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import engine
from app.models.enums import REPORT_ENGINE_VERSION, REPORT_SCHEMA_VERSION
from app.models.report_run import ReportRun
from app.models.task import Task
from app.models.task_run import TaskRun


# ---------------------------------------------------------------------------
# Helpers — build real FK parent rows via API so SQLite FK checks pass
# ---------------------------------------------------------------------------

def _register_and_setup(client: TestClient, suffix: str) -> dict:
    """Register an org and create a data source + REPORT task + run.

    Returns dict with org_id, ds_id, task_id, run_id (all uuid.UUID) and
    a second run_id_2 for tests that need two runs under the same task.
    """
    resp = client.post(
        "/auth/register",
        json={
            "organization_name": f"Model Test Org {suffix}",
            "email": f"model-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Model Tester",
        },
    )
    assert resp.status_code == 201, resp.text
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    ds = client.post(
        "/data-sources",
        json={"name": f"DS {suffix}", "source_type": "csv_upload",
              "connection_metadata": {"file_path": "f.csv"}},
        headers=headers,
    )
    assert ds.status_code == 201, ds.text
    ds_id = uuid.UUID(ds.json()["id"])
    org_id = uuid.UUID(ds.json()["organization_id"])

    task = client.post(
        "/tasks",
        json={"name": f"Report Task {suffix}", "task_type": "report",
              "data_source_id": str(ds_id)},
        headers=headers,
    )
    assert task.status_code == 201, task.text
    task_id = uuid.UUID(task.json()["id"])

    run1 = client.post(f"/tasks/{task_id}/runs", headers=headers)
    assert run1.status_code == 201, run1.text
    run_id = uuid.UUID(run1.json()["id"])

    run2 = client.post(f"/tasks/{task_id}/runs", headers=headers)
    assert run2.status_code == 201, run2.text
    run_id_2 = uuid.UUID(run2.json()["id"])

    return {
        "headers": headers,
        "org_id": org_id,
        "ds_id": ds_id,
        "task_id": task_id,
        "run_id": run_id,
        "run_id_2": run_id_2,
    }


def _make_report_run(org_id: uuid.UUID, task_run_id: uuid.UUID,
                      task_id: uuid.UUID, ds_id: uuid.UUID,
                      qcr_id: uuid.UUID | None = None,
                      rr_id: uuid.UUID | None = None) -> ReportRun:
    return ReportRun(
        id=rr_id or uuid.uuid4(),
        organization_id=org_id,
        task_run_id=task_run_id,
        task_id=task_id,
        data_source_id=ds_id,
        quality_control_run_id=qcr_id,
        report_engine_version=REPORT_ENGINE_VERSION,
        report_schema_version=REPORT_SCHEMA_VERSION,
        report_data={"stub": True},
    )


# ---------------------------------------------------------------------------
# 1-10: Live schema audit
# ---------------------------------------------------------------------------

def _report_runs_columns() -> set[str]:
    insp = inspect(engine)
    return {col["name"] for col in insp.get_columns("report_runs")}


def test_schema_column_id() -> None:
    """1. id column present."""
    assert "id" in _report_runs_columns()


def test_schema_column_organization_id() -> None:
    """2. organization_id column present."""
    assert "organization_id" in _report_runs_columns()


def test_schema_column_task_run_id() -> None:
    """3. task_run_id column present."""
    assert "task_run_id" in _report_runs_columns()


def test_schema_column_task_id() -> None:
    """4. task_id column present."""
    assert "task_id" in _report_runs_columns()


def test_schema_column_data_source_id() -> None:
    """5. data_source_id column present."""
    assert "data_source_id" in _report_runs_columns()


def test_schema_column_quality_control_run_id() -> None:
    """6. quality_control_run_id column present."""
    assert "quality_control_run_id" in _report_runs_columns()


def test_schema_column_report_engine_version() -> None:
    """7. report_engine_version column present."""
    assert "report_engine_version" in _report_runs_columns()


def test_schema_column_report_schema_version() -> None:
    """8. report_schema_version column present."""
    assert "report_schema_version" in _report_runs_columns()


def test_schema_column_report_data() -> None:
    """9. report_data column present."""
    assert "report_data" in _report_runs_columns()


def test_schema_column_created_at() -> None:
    """10. created_at column present."""
    assert "created_at" in _report_runs_columns()


# ---------------------------------------------------------------------------
# 11-12: Nullable checks
# ---------------------------------------------------------------------------

def test_nullable_quality_control_run_id(client, db_session) -> None:
    """11. quality_control_run_id is nullable; row inserts successfully with None."""
    s = uuid.uuid4().hex[:8]
    ids = _register_and_setup(client, s)
    row = _make_report_run(
        ids["org_id"], ids["run_id"], ids["task_id"], ids["ds_id"], qcr_id=None
    )
    db_session.add(row)
    db_session.commit()  # Must not raise
    db_session.refresh(row)
    assert row.quality_control_run_id is None


def test_non_null_report_data(client, db_session) -> None:
    """12. report_data can be an empty dict (NOT NULL but blank dict is valid)."""
    s = uuid.uuid4().hex[:8]
    ids = _register_and_setup(client, s)
    row = ReportRun(
        id=uuid.uuid4(),
        organization_id=ids["org_id"],
        task_run_id=ids["run_id"],
        task_id=ids["task_id"],
        data_source_id=ids["ds_id"],
        quality_control_run_id=None,
        report_engine_version=REPORT_ENGINE_VERSION,
        report_schema_version=REPORT_SCHEMA_VERSION,
        report_data={},
    )
    db_session.add(row)
    db_session.commit()  # Must not raise
    db_session.refresh(row)
    assert row.report_data == {}


# ---------------------------------------------------------------------------
# 13-14: Unique constraint enforcement
# ---------------------------------------------------------------------------

def test_unique_task_run_id_constraint(client, db_session) -> None:
    """13. uq_report_runs_task_run_id: second insert with same task_run_id raises."""
    s = uuid.uuid4().hex[:8]
    ids = _register_and_setup(client, s)
    # row1 uses run_id; row2 tries to reuse the same task_run_id
    row1 = _make_report_run(ids["org_id"], ids["run_id"], ids["task_id"], ids["ds_id"])
    # row2: use run_id_2 as the ReportRun.task_run_id target FK, but same run_id in UNIQUE col
    row2 = _make_report_run(ids["org_id"], ids["run_id"], ids["task_id"], ids["ds_id"])
    db_session.add(row1)
    db_session.commit()
    db_session.add(row2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_unique_org_id_constraint(client, db_session) -> None:
    """14. uq_report_runs_org_id: second insert with same (org, id) raises."""
    s = uuid.uuid4().hex[:8]
    ids = _register_and_setup(client, s)
    shared_id = uuid.uuid4()
    row1 = _make_report_run(
        ids["org_id"], ids["run_id"], ids["task_id"], ids["ds_id"],
        rr_id=shared_id,
    )
    row2 = _make_report_run(
        ids["org_id"], ids["run_id_2"], ids["task_id"], ids["ds_id"],
        rr_id=shared_id,  # same (org_id, id) → UNIQUE violation
    )
    db_session.add(row1)
    db_session.commit()
    db_session.add(row2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# ---------------------------------------------------------------------------
# 15-17: Identifier length audit
# ---------------------------------------------------------------------------

def test_table_name_length() -> None:
    """15. Table name <= 63 characters (PostgreSQL identifier limit)."""
    assert len(ReportRun.__tablename__) <= 63


def test_column_name_lengths() -> None:
    """16. All column names <= 63 characters."""
    insp = inspect(engine)
    for col in insp.get_columns("report_runs"):
        assert len(col["name"]) <= 63, f"Column name too long: {col['name']}"


def test_constraint_name_lengths() -> None:
    """17. All constraint names <= 63 characters."""
    insp = inspect(engine)
    for uc in insp.get_unique_constraints("report_runs"):
        name = uc.get("name") or ""
        assert len(name) <= 63, f"Constraint name too long: {name}"
    for idx in insp.get_indexes("report_runs"):
        name = idx.get("name") or ""
        assert len(name) <= 63, f"Index name too long: {name}"


# ---------------------------------------------------------------------------
# 18: Engine purity
# ---------------------------------------------------------------------------

_REPORTS_PKG = Path(__file__).resolve().parents[1] / "backend" / "app" / "reports"

_FORBIDDEN_ENGINE_IMPORTS = {
    "sqlalchemy",
    "fastapi",
    "app.worker",
    "app.api",
    "app.db",
}


def _collect_imports(source: str) -> set[str]:
    """Parse a Python source string and return the set of top-level imports."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imports.add(node.module)
    return imports


def test_reports_package_purity() -> None:
    """18. app/reports/ package has no SQLAlchemy / FastAPI / worker imports."""
    bad_files: list[str] = []
    for py_file in _REPORTS_PKG.rglob("*.py"):
        source = py_file.read_text(encoding="utf-8")
        imports = _collect_imports(source)
        for imp in imports:
            for forbidden in _FORBIDDEN_ENGINE_IMPORTS:
                if imp == forbidden or imp.startswith(forbidden + "."):
                    bad_files.append(f"{py_file.name}: imports {imp!r}")
                    break
    assert not bad_files, (
        "app/reports/ contains forbidden imports (should be pure):\n"
        + "\n".join(bad_files)
    )
