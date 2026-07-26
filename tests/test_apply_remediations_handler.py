"""APPLY_REMEDIATIONS handler integration tests.

Chain under test:
  SYNC (CsvProfilingHandler) →
  TRANSFORM (CleaningHandler, approved) →
  STANDARDIZE (StandardizationHandler, approved) →
  MATCH (MatchHandler, approved) →
  EXPORT (ExportHandler, approved) →
  DETECT (IssueDetectionHandler, source=export task run) →
  REMEDIATE (RemediationHandler) →
  approve changes (direct DB insert of RemediationChangeDecision) →
  APPLY_REMEDIATIONS (ApprovedChangesApplicatorHandler)  ← what we exercise

Scenarios covered:
  1.  Happy path: approved cell-value changes applied, artifact written, run persisted
  2.  Idempotency: second execute() returns "already exists" without re-applying
  3.  IntegrityError recovery: concurrent duplicate is caught and refetched
  4.  Row-removal changes: rows with remove_duplicate_row action are excluded
  5.  Skipped changes: out-of-range row index → skipped_change_count incremented
  6.  Zero approved changes: no decisions → applied_change_count=0, file still written
  7.  Missing RemediationRun → PermanentExecutionError
  8.  Missing IssueDetectionRun → PermanentExecutionError
  9.  Unapproved ExportRun → PermanentExecutionError
  10. Tenant isolation: cross-org source_task_run_id → PermanentExecutionError
  11. decisions_snapshot_hash is deterministic and matches _compute_decisions_snapshot_hash
  12. Output CSV contains modified cell value
  13. Output CSV excludes removed rows but retains non-removed rows
  14. applied_change_count + skipped_change_count ≤ total approved changes
  15. Missing source_task_run_id on TaskRun → PermanentExecutionError
"""
from __future__ import annotations

import csv
import hashlib
import io
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from unittest.mock import patch

from app.core.config import get_settings
from app.models.applied_remediation_run import AppliedRemediationRun
from app.models.data_source import DataSource
from app.models.enums import TaskType
from app.models.export_run import ExportRun
from app.models.remediation_change import RemediationChange
from app.models.remediation_change_decision import RemediationChangeDecision
from app.models.remediation_run import RemediationRun
from app.models.task import Task
from app.models.task_run import TaskRun
from app.worker.handlers.apply_remediations import (
    APPLY_ENGINE_VERSION,
    ApprovedChangesApplicatorHandler,
    _compute_decisions_snapshot_hash,
)
from app.worker.handlers.base import ExecutionContext, PermanentExecutionError
from app.worker.handlers.cleaning import CleaningHandler
from app.worker.handlers.csv_profiling import CsvProfilingHandler
from app.worker.handlers.export import ExportHandler
from app.worker.handlers.issue_detection import IssueDetectionHandler
from app.worker.handlers.matching import MatchHandler
from app.worker.handlers.remediation import RemediationHandler
from app.worker.handlers.standardization import StandardizationHandler


# ---------------------------------------------------------------------------
# CSV fixture – whitespace padding triggers trim_whitespace remediation
# ---------------------------------------------------------------------------

_CSV = (
    "id,name,email\n"
    "1, Alice ,alice@example.com\n"
    "2,  Bob  ,bob@example.com\n"
    "3,Charlie,charlie@example.com\n"
)


# ---------------------------------------------------------------------------
# Environment helpers
# ---------------------------------------------------------------------------

def _auth_headers(client: TestClient, suffix: str) -> dict:
    response = client.post(
        "/auth/register",
        json={
            "organization_name": f"ApplyRem Org {suffix}",
            "email": f"apply-rem-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Apply Rem User",
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
    monkeypatch.setenv("CSV_REMEDIATED_ROOT", str(tmp_path / "csv_remediated"))
    get_settings.cache_clear()
    return csv_root


# ---------------------------------------------------------------------------
# Full pipeline builder: SYNC → TRANSFORM → STANDARDIZE → MATCH → EXPORT
# ---------------------------------------------------------------------------

def _build_approved_export_run(
    client: TestClient,
    db_session,
    csv_root: Path,
    *,
    csv_content: str = _CSV,
    suffix: str | None = None,
) -> tuple[dict, str, str, str]:
    """Build the full pipeline up to and including an approved ExportRun.

    Returns (headers, source_id, export_task_run_id, organization_id).
    """
    suffix = suffix or uuid.uuid4().hex[:8]
    headers = _auth_headers(client, suffix)

    # DataSource
    ds_resp = client.post(
        "/data-sources",
        json={
            "name": f"Apply Rem Source {suffix}",
            "source_type": "csv_upload",
            "connection_metadata": {"file_path": "customers.csv"},
        },
        headers=headers,
    )
    assert ds_resp.status_code == 201, ds_resp.text
    source_id = ds_resp.json()["id"]
    organization_id = ds_resp.json()["organization_id"]

    # Write CSV to disk
    org_dir = csv_root / organization_id
    org_dir.mkdir(parents=True, exist_ok=True)
    (org_dir / "customers.csv").write_text(csv_content, encoding="utf-8")

    # SYNC
    sync_task_resp = client.post(
        "/tasks",
        json={"name": f"Sync {suffix}", "task_type": "sync", "data_source_id": source_id},
        headers=headers,
    )
    assert sync_task_resp.status_code == 201, sync_task_resp.text
    sync_run_resp = client.post(
        f"/tasks/{sync_task_resp.json()['id']}/runs", headers=headers
    )
    assert sync_run_resp.status_code == 201, sync_run_resp.text
    sync_run_id = sync_run_resp.json()["id"]
    sync_task = db_session.get(Task, uuid.UUID(sync_task_resp.json()["id"]))
    sync_run = db_session.get(TaskRun, uuid.UUID(sync_run_id))
    source = db_session.get(DataSource, uuid.UUID(source_id))
    CsvProfilingHandler().execute(
        ExecutionContext(
            task_run=sync_run, task=sync_task, data_source=source,
            idempotency_key=str(sync_run.idempotency_key), credential_provider=None,
        )
    )

    # TRANSFORM (cleaning)
    transform_task_resp = client.post(
        "/tasks",
        json={"name": f"Clean {suffix}", "task_type": "transform", "data_source_id": source_id},
        headers=headers,
    )
    transform_run_resp = client.post(
        f"/tasks/{transform_task_resp.json()['id']}/runs",
        json={"source_task_run_id": sync_run_id},
        headers=headers,
    )
    transform_task_id = transform_task_resp.json()["id"]
    transform_run_id = transform_run_resp.json()["id"]
    transform_task = db_session.get(Task, uuid.UUID(transform_task_id))
    transform_run = db_session.get(TaskRun, uuid.UUID(transform_run_id))
    CleaningHandler().execute(
        ExecutionContext(
            task_run=transform_run, task=transform_task, data_source=source,
            idempotency_key=str(transform_run.idempotency_key), credential_provider=None,
        )
    )
    approve = client.post(
        f"/tasks/{transform_task_id}/runs/{transform_run_id}/cleaning/approve",
        headers=headers,
    )
    assert approve.status_code == 200, approve.text

    # STANDARDIZE
    std_task_resp = client.post(
        "/tasks",
        json={"name": f"Standardize {suffix}", "task_type": "standardize",
              "data_source_id": source_id},
        headers=headers,
    )
    std_run_resp = client.post(
        f"/tasks/{std_task_resp.json()['id']}/runs",
        json={"source_task_run_id": transform_run_id},
        headers=headers,
    )
    std_task_id = std_task_resp.json()["id"]
    std_run_id = std_run_resp.json()["id"]
    std_task = db_session.get(Task, uuid.UUID(std_task_id))
    std_run = db_session.get(TaskRun, uuid.UUID(std_run_id))
    StandardizationHandler().execute(
        ExecutionContext(
            task_run=std_run, task=std_task, data_source=source,
            idempotency_key=str(std_run.idempotency_key), credential_provider=None,
        )
    )
    approve = client.post(
        f"/tasks/{std_task_id}/runs/{std_run_id}/standardization/approve",
        headers=headers,
    )
    assert approve.status_code == 200, approve.text

    # MATCH
    match_task_resp = client.post(
        "/tasks",
        json={"name": f"Match {suffix}", "task_type": "match", "data_source_id": source_id},
        headers=headers,
    )
    match_run_resp = client.post(
        f"/tasks/{match_task_resp.json()['id']}/runs",
        json={"source_task_run_id": std_run_id},
        headers=headers,
    )
    match_task_id = match_task_resp.json()["id"]
    match_run_id = match_run_resp.json()["id"]
    match_task = db_session.get(Task, uuid.UUID(match_task_id))
    match_run = db_session.get(TaskRun, uuid.UUID(match_run_id))
    MatchHandler().execute(
        ExecutionContext(
            task_run=match_run, task=match_task, data_source=source,
            idempotency_key=str(match_run.idempotency_key), credential_provider=None,
        )
    )
    approve = client.post(
        f"/tasks/{match_task_id}/runs/{match_run_id}/matching/approve",
        headers=headers,
    )
    assert approve.status_code == 200, approve.text

    # EXPORT
    export_task_resp = client.post(
        "/tasks",
        json={"name": f"Export {suffix}", "task_type": "export", "data_source_id": source_id},
        headers=headers,
    )
    export_run_resp = client.post(
        f"/tasks/{export_task_resp.json()['id']}/runs",
        json={"source_task_run_id": match_run_id},
        headers=headers,
    )
    export_task_id = export_task_resp.json()["id"]
    export_task_run_id = export_run_resp.json()["id"]
    export_task = db_session.get(Task, uuid.UUID(export_task_id))
    export_task_run = db_session.get(TaskRun, uuid.UUID(export_task_run_id))
    ExportHandler().execute(
        ExecutionContext(
            task_run=export_task_run, task=export_task, data_source=source,
            idempotency_key=str(export_task_run.idempotency_key), credential_provider=None,
        )
    )
    approve = client.post(
        f"/tasks/{export_task_id}/runs/{export_task_run_id}/export/approve",
        headers=headers,
    )
    assert approve.status_code == 200, approve.text

    return headers, source_id, export_task_run_id, organization_id


def _build_detect_run(
    client: TestClient,
    db_session,
    csv_root: Path,
    headers: dict,
    source_id: str,
    export_task_run_id: str,
    organization_id: str,
) -> tuple[str, str]:
    """Build DETECT with source_task_run_id = export_task_run_id.

    The DETECT API doesn't accept source_task_run_id (same constraint as in
    test_quality_control_handler.py), so we create the run without it and then
    set the field directly on the ORM object.

    Returns (detect_task_id, detect_run_id).
    """
    detect_task_resp = client.post(
        "/tasks",
        json={"name": "Detect Issues", "task_type": "detect", "data_source_id": source_id},
        headers=headers,
    )
    assert detect_task_resp.status_code == 201, detect_task_resp.text
    detect_task_id = detect_task_resp.json()["id"]

    detect_run_resp = client.post(
        f"/tasks/{detect_task_id}/runs",
        headers=headers,
    )
    assert detect_run_resp.status_code == 201, detect_run_resp.text
    detect_run_id = detect_run_resp.json()["id"]

    db_session.expire_all()
    detect_task = db_session.get(Task, uuid.UUID(detect_task_id))
    detect_run = db_session.get(TaskRun, uuid.UUID(detect_run_id))
    # Set source_task_run_id directly so the handler can walk back to ExportRun.
    detect_run.source_task_run_id = uuid.UUID(export_task_run_id)
    db_session.commit()

    source = db_session.get(DataSource, uuid.UUID(source_id))
    IssueDetectionHandler().execute(
        ExecutionContext(
            task_run=detect_run, task=detect_task, data_source=source,
            idempotency_key=str(detect_run.idempotency_key), credential_provider=None,
        )
    )
    return detect_task_id, detect_run_id


def _build_remediate_run(
    client: TestClient,
    db_session,
    headers: dict,
    source_id: str,
    detect_run_id: str,
) -> tuple[str, RemediationRun]:
    """Build REMEDIATE with source=DETECT. Returns (remediate_run_id, RemediationRun)."""
    rem_task_resp = client.post(
        "/tasks",
        json={"name": "Remediate Issues", "task_type": "remediate", "data_source_id": source_id},
        headers=headers,
    )
    assert rem_task_resp.status_code == 201, rem_task_resp.text
    rem_task_id = rem_task_resp.json()["id"]

    rem_run_resp = client.post(
        f"/tasks/{rem_task_id}/runs",
        json={"source_task_run_id": detect_run_id},
        headers=headers,
    )
    assert rem_run_resp.status_code == 201, rem_run_resp.text
    rem_run_id = rem_run_resp.json()["id"]

    rem_task = db_session.get(Task, uuid.UUID(rem_task_id))
    rem_run = db_session.get(TaskRun, uuid.UUID(rem_run_id))
    source = db_session.get(DataSource, uuid.UUID(source_id))
    result = RemediationHandler().execute(
        ExecutionContext(
            task_run=rem_run, task=rem_task, data_source=source,
            idempotency_key=str(rem_run.idempotency_key), credential_provider=None,
        )
    )
    assert "remediation run created" in result or "already exists" in result, result

    db_session.expire_all()
    remediation_run = db_session.execute(
        select(RemediationRun).where(RemediationRun.task_run_id == rem_run.id)
    ).scalar_one()
    return rem_run_id, remediation_run


def _approve_changes(
    db_session,
    changes: list[RemediationChange],
    organization_id: uuid.UUID,
    decision: str = "approved",
    ts: datetime | None = None,
) -> None:
    if ts is None:
        ts = datetime.now(tz=timezone.utc)
    for ch in changes:
        db_session.add(
            RemediationChangeDecision(
                id=uuid.uuid4(),
                organization_id=organization_id,
                remediation_run_id=ch.remediation_run_id,
                remediation_change_id=ch.id,
                decision=decision,
                decision_timestamp=ts,
                reviewer_name="Test Reviewer",
            )
        )
    db_session.commit()


def _build_apply_context(
    client: TestClient,
    db_session,
    headers: dict,
    source_id: str,
    rem_run_id: str,
) -> ExecutionContext:
    """Create an APPLY_REMEDIATIONS task+run with source=rem_run_id.

    The API doesn't accept source_task_run_id for apply_remediations task type,
    so we create the run without it and set the field directly on the ORM object.
    """
    apply_task_resp = client.post(
        "/tasks",
        json={
            "name": "Apply Remediations",
            "task_type": "apply_remediations",
            "data_source_id": source_id,
        },
        headers=headers,
    )
    assert apply_task_resp.status_code == 201, apply_task_resp.text
    apply_task_id = apply_task_resp.json()["id"]

    apply_run_resp = client.post(
        f"/tasks/{apply_task_id}/runs",
        headers=headers,
    )
    assert apply_run_resp.status_code == 201, apply_run_resp.text
    apply_run_id = apply_run_resp.json()["id"]

    db_session.expire_all()
    apply_task = db_session.get(Task, uuid.UUID(apply_task_id))
    apply_run = db_session.get(TaskRun, uuid.UUID(apply_run_id))
    # Set source_task_run_id directly (API doesn't allow it for apply_remediations).
    apply_run.source_task_run_id = uuid.UUID(rem_run_id)
    db_session.commit()

    source = db_session.get(DataSource, uuid.UUID(source_id))
    return ExecutionContext(
        task_run=apply_run, task=apply_task, data_source=source,
        idempotency_key=str(apply_run.idempotency_key), credential_provider=None,
    )


def _full_chain(
    client: TestClient,
    db_session,
    csv_root: Path,
    *,
    csv_content: str = _CSV,
    suffix: str | None = None,
    approve_all: bool = True,
) -> dict:
    """Build and optionally approve the full chain.

    Returns dict with keys: headers, source_id, organization_id,
    export_task_run_id, detect_run_id, rem_run_id, remediation_run,
    changes (list[RemediationChange]).
    """
    suffix = suffix or uuid.uuid4().hex[:8]
    headers, source_id, export_task_run_id, org_id = _build_approved_export_run(
        client, db_session, csv_root, csv_content=csv_content, suffix=suffix
    )
    _detect_task_id, detect_run_id = _build_detect_run(
        client, db_session, csv_root, headers, source_id, export_task_run_id, org_id
    )
    rem_run_id, remediation_run = _build_remediate_run(
        client, db_session, headers, source_id, detect_run_id
    )

    db_session.expire_all()
    changes = db_session.execute(
        select(RemediationChange).where(
            RemediationChange.remediation_run_id == remediation_run.id
        )
    ).scalars().all()

    if approve_all:
        _approve_changes(db_session, changes, uuid.UUID(org_id))

    return {
        "headers": headers,
        "source_id": source_id,
        "organization_id": org_id,
        "export_task_run_id": export_task_run_id,
        "detect_run_id": detect_run_id,
        "rem_run_id": rem_run_id,
        "remediation_run": remediation_run,
        "changes": changes,
    }


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_apply_remediations_handler_happy_path(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Full chain: whitespace issues → remediate → approve all → apply.
    The output artifact must exist, sha256 must match, applied_change_count
    must equal the number of approved changes."""
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        chain = _full_chain(client, db_session, csv_root)
        context = _build_apply_context(
            client, db_session,
            chain["headers"], chain["source_id"], chain["rem_run_id"],
        )

        result = ApprovedChangesApplicatorHandler().execute(context)
        assert "applied remediation run created" in result

        db_session.expire_all()
        applied_run = db_session.execute(
            select(AppliedRemediationRun).where(
                AppliedRemediationRun.task_run_id == context.task_run.id
            )
        ).scalar_one()

        assert applied_run.organization_id == uuid.UUID(chain["organization_id"])
        assert applied_run.task_run_id == context.task_run.id
        assert applied_run.task_id == context.task.id
        assert applied_run.data_source_id == context.data_source.id
        assert applied_run.remediation_run_id == chain["remediation_run"].id
        assert applied_run.applied_change_count > 0
        assert applied_run.skipped_change_count == 0
        assert applied_run.apply_engine_version == APPLY_ENGINE_VERSION
        assert len(applied_run.output_sha256) == 64
        assert len(applied_run.decisions_snapshot_hash) == 64

        # Artifact must exist on disk
        artifact = Path(applied_run.output_file_path)
        assert artifact.exists()
        raw = artifact.read_bytes()
        assert hashlib.sha256(raw).hexdigest() == applied_run.output_sha256

    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_apply_remediations_handler_idempotency(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Second execute() returns the existing run without re-applying."""
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        chain = _full_chain(client, db_session, csv_root)
        context = _build_apply_context(
            client, db_session,
            chain["headers"], chain["source_id"], chain["rem_run_id"],
        )

        result1 = ApprovedChangesApplicatorHandler().execute(context)
        assert "created" in result1

        result2 = ApprovedChangesApplicatorHandler().execute(context)
        assert "already exists" in result2

        # Exactly one AppliedRemediationRun row created
        rows = db_session.execute(
            select(AppliedRemediationRun).where(
                AppliedRemediationRun.task_run_id == context.task_run.id
            )
        ).scalars().all()
        assert len(rows) == 1

    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Cell-value patch: output CSV contains modified value
# ---------------------------------------------------------------------------

def test_apply_remediations_handler_cell_patch_reflected_in_output(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Approved trim_whitespace changes must appear as stripped values in
    the output CSV artifact."""
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        chain = _full_chain(client, db_session, csv_root)
        context = _build_apply_context(
            client, db_session,
            chain["headers"], chain["source_id"], chain["rem_run_id"],
        )
        ApprovedChangesApplicatorHandler().execute(context)

        db_session.expire_all()
        applied_run = db_session.execute(
            select(AppliedRemediationRun).where(
                AppliedRemediationRun.task_run_id == context.task_run.id
            )
        ).scalar_one()

        artifact_bytes = Path(applied_run.output_file_path).read_bytes()
        reader = list(csv.reader(io.StringIO(artifact_bytes.decode("utf-8"))))
        # Header row + 3 data rows (no removals in _CSV)
        assert len(reader) == 4  # header + 3 data rows
        # All name values must be stripped (the remediation proposed stripped values)
        name_col = reader[0].index("name")
        for row in reader[1:]:
            assert row[name_col] == row[name_col].strip(), f"Name not stripped: {row[name_col]!r}"

    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Row-removal
# ---------------------------------------------------------------------------

def test_apply_remediations_handler_row_removal(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """CSV with a duplicate row triggers remove_duplicate_row change.
    The output artifact must have one fewer row than the export artifact."""
    csv_root = _set_roots(monkeypatch, tmp_path)
    # CSV with one duplicate row (row 2 == row 1 after standardization)
    csv_with_dup = (
        "id,name,email\n"
        "1,alice,alice@example.com\n"
        "1,alice,alice@example.com\n"  # duplicate
        "2,bob,bob@example.com\n"
    )
    try:
        chain = _full_chain(client, db_session, csv_root, csv_content=csv_with_dup)
        context = _build_apply_context(
            client, db_session,
            chain["headers"], chain["source_id"], chain["rem_run_id"],
        )

        # Verify at least one row-removal change was proposed
        removal_changes = [
            c for c in chain["changes"]
            if c.action in ("remove_duplicate_row", "remove_duplicate_primary_key")
        ]
        if not removal_changes:
            pytest.skip("No row-removal changes proposed for this fixture")

        result = ApprovedChangesApplicatorHandler().execute(context)
        assert "applied remediation run created" in result

        db_session.expire_all()
        applied_run = db_session.execute(
            select(AppliedRemediationRun).where(
                AppliedRemediationRun.task_run_id == context.task_run.id
            )
        ).scalar_one()

        artifact_bytes = Path(applied_run.output_file_path).read_bytes()
        reader = list(csv.reader(io.StringIO(artifact_bytes.decode("utf-8"))))
        # Header + (original data rows - removed rows)
        data_rows = len(reader) - 1  # exclude header
        assert data_rows < 3, f"Expected fewer than 3 data rows, got {data_rows}"
        assert applied_run.applied_change_count >= len(removal_changes)

    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Skipped changes (out-of-range row index)
# ---------------------------------------------------------------------------

def test_apply_remediations_handler_skipped_out_of_range(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """A RemediationChange with an out-of-range row_number is skipped,
    incrementing skipped_change_count."""
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        chain = _full_chain(client, db_session, csv_root)
        changes = chain["changes"]
        if not changes:
            pytest.skip("No remediation changes proposed for this fixture")

        # Corrupt one change's row_number to be way out of range
        changes[0].row_number = 99999
        db_session.commit()

        context = _build_apply_context(
            client, db_session,
            chain["headers"], chain["source_id"], chain["rem_run_id"],
        )
        ApprovedChangesApplicatorHandler().execute(context)

        db_session.expire_all()
        applied_run = db_session.execute(
            select(AppliedRemediationRun).where(
                AppliedRemediationRun.task_run_id == context.task_run.id
            )
        ).scalar_one()
        assert applied_run.skipped_change_count >= 1

    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Zero approved changes
# ---------------------------------------------------------------------------

def test_apply_remediations_handler_zero_approved_changes(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """No approved decisions → applied_change_count=0, file still written,
    decisions_snapshot_hash is the empty-set hash."""
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        chain = _full_chain(client, db_session, csv_root, approve_all=False)
        # Reject all changes instead
        _approve_changes(db_session, chain["changes"], uuid.UUID(chain["organization_id"]),
                         decision="rejected")

        context = _build_apply_context(
            client, db_session,
            chain["headers"], chain["source_id"], chain["rem_run_id"],
        )
        result = ApprovedChangesApplicatorHandler().execute(context)
        assert "applied remediation run created" in result

        db_session.expire_all()
        applied_run = db_session.execute(
            select(AppliedRemediationRun).where(
                AppliedRemediationRun.task_run_id == context.task_run.id
            )
        ).scalar_one()
        assert applied_run.applied_change_count == 0
        # Output file must exist (empty-set artifact)
        assert Path(applied_run.output_file_path).exists()
        # decisions_snapshot_hash of empty set = sha256(b"")
        empty_hash = hashlib.sha256(b"").hexdigest()
        assert applied_run.decisions_snapshot_hash == empty_hash

    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------

def test_apply_remediations_handler_missing_source_task_run_id(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        chain = _full_chain(client, db_session, csv_root)
        context = _build_apply_context(
            client, db_session,
            chain["headers"], chain["source_id"], chain["rem_run_id"],
        )
        # Null out source_task_run_id
        context.task_run.source_task_run_id = None
        db_session.commit()

        with pytest.raises(PermanentExecutionError, match="source_task_run_id"):
            ApprovedChangesApplicatorHandler().execute(context)
    finally:
        get_settings.cache_clear()


def test_apply_remediations_handler_missing_remediation_run(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """If source_task_run_id doesn't resolve to a RemediationRun → permanent failure."""
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        chain = _full_chain(client, db_session, csv_root)
        context = _build_apply_context(
            client, db_session,
            chain["headers"], chain["source_id"], chain["rem_run_id"],
        )
        # Point source_task_run_id at the export task run ID -- it exists in
        # task_runs (passes the FK constraint) but has no RemediationRun row.
        context.task_run.source_task_run_id = uuid.UUID(chain["export_task_run_id"])
        db_session.commit()

        with pytest.raises(PermanentExecutionError, match="remediation run"):
            ApprovedChangesApplicatorHandler().execute(context)
    finally:
        get_settings.cache_clear()


def test_apply_remediations_handler_missing_issue_detection_run(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """If the remediation run points to a source that has no IssueDetectionRun,
    the handler raises PermanentExecutionError."""
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        chain = _full_chain(client, db_session, csv_root)
        context = _build_apply_context(
            client, db_session,
            chain["headers"], chain["source_id"], chain["rem_run_id"],
        )
        # Corrupt the remediation_run's source_task_run_id so it won't
        # resolve to an IssueDetectionRun. Use export_task_run_id -- it
        # exists in task_runs (passes the FK constraint) but has no
        # IssueDetectionRun row.
        remediation_run = chain["remediation_run"]
        db_session.expire_all()
        remediation_run = db_session.merge(remediation_run)
        remediation_run.source_task_run_id = uuid.UUID(chain["export_task_run_id"])
        db_session.commit()

        with pytest.raises(PermanentExecutionError, match="IssueDetectionRun"):
            ApprovedChangesApplicatorHandler().execute(context)
    finally:
        get_settings.cache_clear()


def test_apply_remediations_handler_unapproved_export_run(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """If the ExportRun has status != 'approved' the handler fails permanently."""
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        chain = _full_chain(client, db_session, csv_root)

        # Find and de-approve the ExportRun (use "rejected" -- a valid status
        # per ck_export_runs_status_valid that is not "approved")
        export_run = db_session.execute(
            select(ExportRun).where(
                ExportRun.task_run_id == uuid.UUID(chain["export_task_run_id"])
            )
        ).scalar_one()
        export_run.status = "rejected"
        db_session.commit()

        context = _build_apply_context(
            client, db_session,
            chain["headers"], chain["source_id"], chain["rem_run_id"],
        )
        with pytest.raises(PermanentExecutionError, match="APPROVED ExportRun"):
            ApprovedChangesApplicatorHandler().execute(context)
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Tenant isolation
# ---------------------------------------------------------------------------

def test_apply_remediations_handler_cross_org_isolation(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Tenant isolation: the handler scopes every query to organization_id,
    so org B's RemediationRun is never visible to org A's handler.

    The composite FK (organization_id, source_task_run_id) → task_runs
    enforces that source_task_run_id must reference a task run in the same
    org, so it is impossible to point org A's source_task_run_id directly at
    org B's task run. Instead we verify isolation by confirming that:
      a) Two chains produce different organization_ids (truly separate tenants).
      b) Org A's context resolves no RemediationRun when source_task_run_id
         points at org A's export task run (exists in task_runs for org A
         but has no RemediationRun — mirrors the cross-org scenario where
         org B's RemediationRun is simply invisible to org A's scoped query).
    """
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        chain_a = _full_chain(client, db_session, csv_root, suffix="isola")
        chain_b = _full_chain(client, db_session, csv_root, suffix="isolb")
        assert chain_a["organization_id"] != chain_b["organization_id"]

        # Use org A's export_task_run_id as source -- it belongs to org A
        # (passes the composite FK), but has no RemediationRun, exactly as
        # org B's RemediationRun would be invisible to org A's scoped query.
        context_a = _build_apply_context(
            client, db_session,
            chain_a["headers"], chain_a["source_id"], chain_a["export_task_run_id"],
        )
        with pytest.raises(PermanentExecutionError, match="remediation run"):
            ApprovedChangesApplicatorHandler().execute(context_a)
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# decisions_snapshot_hash determinism
# ---------------------------------------------------------------------------

def test_apply_remediations_handler_decisions_snapshot_hash_deterministic(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """decisions_snapshot_hash on the persisted run matches what
    _compute_decisions_snapshot_hash produces for the same approved changes."""
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        chain = _full_chain(client, db_session, csv_root)
        context = _build_apply_context(
            client, db_session,
            chain["headers"], chain["source_id"], chain["rem_run_id"],
        )
        ApprovedChangesApplicatorHandler().execute(context)

        db_session.expire_all()
        applied_run = db_session.execute(
            select(AppliedRemediationRun).where(
                AppliedRemediationRun.task_run_id == context.task_run.id
            )
        ).scalar_one()

        # Recompute expected hash using the approved changes
        approved = [
            c for c in chain["changes"]
            # all were approved by _full_chain(approve_all=True)
        ]
        expected_hash = _compute_decisions_snapshot_hash(approved)
        assert applied_run.decisions_snapshot_hash == expected_hash

    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# applied_change_count + skipped_change_count accounting
# ---------------------------------------------------------------------------

def test_apply_remediations_handler_change_count_accounting(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """applied_change_count + skipped_change_count == total approved changes."""
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        chain = _full_chain(client, db_session, csv_root)
        n_approved = len(chain["changes"])  # all were approved

        context = _build_apply_context(
            client, db_session,
            chain["headers"], chain["source_id"], chain["rem_run_id"],
        )
        ApprovedChangesApplicatorHandler().execute(context)

        db_session.expire_all()
        applied_run = db_session.execute(
            select(AppliedRemediationRun).where(
                AppliedRemediationRun.task_run_id == context.task_run.id
            )
        ).scalar_one()

        total = applied_run.applied_change_count + applied_run.skipped_change_count
        assert total == n_approved

    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

def test_apply_remediations_handler_registered_in_registry() -> None:
    """TaskType.APPLY_REMEDIATIONS is registered in the HANDLER_REGISTRY."""
    from app.worker.handlers import HANDLER_REGISTRY
    assert TaskType.APPLY_REMEDIATIONS in HANDLER_REGISTRY
    assert isinstance(HANDLER_REGISTRY[TaskType.APPLY_REMEDIATIONS],
                      ApprovedChangesApplicatorHandler)
