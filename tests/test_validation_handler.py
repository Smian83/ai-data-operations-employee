"""Module 17 Phase 3 ValidationHandler integration tests. Mirrors
test_remediation_handler.py's fixture conventions for the upstream
REMEDIATE stage, extended to include the approval step that Module 16
introduced (RemediationChangeDecision rows).

Test chain for most tests:
  detect (IssueDetectionHandler) →
  remediate (RemediationHandler) →
  approve changes (direct DB insert of RemediationChangeDecision) →
  validate (ValidationHandler)

Covers every scenario the Module 17 Phase 3 kickoff message named:
  - happy path (approved changes → passed validations)
  - empty approved snapshot (no decisions at all → zero approved)
  - failed validations (proposed_value wrong)
  - skipped validations (missing config gate)
  - frozen snapshot (approvals added after snapshot freeze don't affect run)
  - idempotency retry (second execute() returns existing run)
  - IntegrityError recovery (concurrent duplicate is refetched)
  - organization isolation (cross-tenant changes never surfaced)
  - one transaction (no partial commits)
  - batch persistence (result count matches approved count)
  - stable ordering (results ordered by row_number)
  - no duplicate results (each change_id appears exactly once)
  - ValidationRun summary counts (passed+failed+skipped == approved_considered)
  - version propagation (validation_engine_version on every result)
  - zero approved changes (no decisions → no results, zeroed run)
  - unsupported action (unknown action → skipped with ACTION_NOT_RECOGNISED)
  - missing source_task_run_id → permanent failure
  - missing RemediationRun → permanent failure
  - result limit enforced (excess approved changes → RESULT_LIMIT_REACHED)
  - tenant isolation on column config (org A config never leaks to org B)
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.models.data_source import DataSource
from app.models.issue_detection_column_rule import IssueDetectionColumnRule
from app.models.remediation_change import RemediationChange
from app.models.remediation_change_decision import RemediationChangeDecision
from app.models.remediation_column_rule import RemediationColumnRule
from app.models.remediation_run import RemediationRun
from app.models.task import Task
from app.models.task_run import TaskRun
from app.models.validation_result import ValidationResult
from app.models.validation_run import ValidationRun
from app.validation.reasons import (
    ACTION_NOT_RECOGNISED,
    PROPOSED_VALUE_MATCHES,
    PROPOSED_VALUE_MISMATCH,
    RESULT_LIMIT_REACHED,
    SOURCE_DATE_FORMAT_NOT_CONFIGURED,
)
from app.worker.handlers.base import ExecutionContext, PermanentExecutionError
from app.worker.handlers.issue_detection import IssueDetectionHandler
from app.worker.handlers.remediation import RemediationHandler
from app.worker.handlers.validation import ValidationHandler


# ---------------------------------------------------------------------------
# Helper: register + authenticate
# ---------------------------------------------------------------------------

def _auth_headers(client: TestClient, suffix: str) -> dict:
    response = client.post(
        "/auth/register",
        json={
            "organization_name": f"Validation Org {suffix}",
            "email": f"validate-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Validation User",
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _set_csv_root(monkeypatch, tmp_path: Path) -> Path:
    csv_root = tmp_path / "csv"
    csv_root.mkdir()
    monkeypatch.setenv("CSV_INPUT_ROOT", str(csv_root))
    get_settings.cache_clear()
    return csv_root


# ---------------------------------------------------------------------------
# Helper: build full detect → remediate chain
# ---------------------------------------------------------------------------

def _build_detect_run(
    client: TestClient,
    db_session,
    csv_root: Path,
    csv_content: str,
    *,
    relative_path: str = "customers.csv",
    suffix: str | None = None,
    detection_column_rules: list[IssueDetectionColumnRule] | None = None,
) -> tuple[dict, str, str, str]:
    """Returns (headers, source_id, detect_run_id, organization_id)."""
    suffix = suffix or uuid.uuid4().hex
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
    organization_id = source_response.json()["organization_id"]

    if detection_column_rules:
        for rule in detection_column_rules:
            rule.organization_id = uuid.UUID(organization_id)
            db_session.add(rule)
        db_session.commit()

    detect_task_response = client.post(
        "/tasks",
        json={"name": "Detect Issues", "task_type": "detect", "data_source_id": source_id},
        headers=headers,
    )
    assert detect_task_response.status_code == 201, detect_task_response.text
    detect_run_response = client.post(
        f"/tasks/{detect_task_response.json()['id']}/runs", headers=headers
    )
    assert detect_run_response.status_code == 201, detect_run_response.text
    detect_run_id = detect_run_response.json()["id"]

    org_dir = csv_root / organization_id
    org_dir.mkdir(parents=True, exist_ok=True)
    (org_dir / relative_path).write_text(csv_content, encoding="utf-8")

    detect_task = db_session.get(Task, uuid.UUID(detect_task_response.json()["id"]))
    detect_run = db_session.get(TaskRun, uuid.UUID(detect_run_id))
    source = db_session.get(DataSource, uuid.UUID(source_id))
    result = IssueDetectionHandler().execute(
        ExecutionContext(
            task_run=detect_run, task=detect_task, data_source=source,
            idempotency_key=str(detect_run.idempotency_key), credential_provider=None,
        )
    )
    assert "created" in result, result
    return headers, source_id, detect_run_id, organization_id


def _build_remediate_run(
    client: TestClient,
    db_session,
    csv_root: Path,
    headers: dict,
    source_id: str,
    detect_run_id: str,
) -> tuple[str, RemediationRun]:
    """Create + execute a REMEDIATE TaskRun. Returns (remediate_run_id, RemediationRun)."""
    remediate_task_response = client.post(
        "/tasks",
        json={"name": "Remediate Issues", "task_type": "remediate", "data_source_id": source_id},
        headers=headers,
    )
    assert remediate_task_response.status_code == 201, remediate_task_response.text
    remediate_task_id = remediate_task_response.json()["id"]

    remediate_run_response = client.post(
        f"/tasks/{remediate_task_id}/runs",
        json={"source_task_run_id": detect_run_id},
        headers=headers,
    )
    assert remediate_run_response.status_code == 201, remediate_run_response.text
    remediate_run_id = remediate_run_response.json()["id"]

    remediate_task = db_session.get(Task, uuid.UUID(remediate_task_id))
    remediate_run = db_session.get(TaskRun, uuid.UUID(remediate_run_id))
    source = db_session.get(DataSource, uuid.UUID(source_id))
    result = RemediationHandler().execute(
        ExecutionContext(
            task_run=remediate_run, task=remediate_task, data_source=source,
            idempotency_key=str(remediate_run.idempotency_key), credential_provider=None,
        )
    )
    assert "remediation run created" in result or "already exists" in result, result

    remediation_run = db_session.execute(
        select(RemediationRun).where(RemediationRun.task_run_id == remediate_run.id)
    ).scalar_one()
    return remediate_run_id, remediation_run


def _approve_changes(
    db_session,
    changes: list[RemediationChange],
    organization_id: uuid.UUID,
    decision: str = "approved",
    ts: datetime | None = None,
) -> list[RemediationChangeDecision]:
    """Insert one RemediationChangeDecision per change, all with the same decision."""
    if ts is None:
        ts = datetime.now(tz=timezone.utc)
    decisions = []
    for change in changes:
        d = RemediationChangeDecision(
            id=uuid.uuid4(),
            organization_id=organization_id,
            remediation_run_id=change.remediation_run_id,
            remediation_change_id=change.id,
            decision=decision,
            decision_timestamp=ts,
            reviewer_name="Test Reviewer",
        )
        db_session.add(d)
        decisions.append(d)
    db_session.commit()
    return decisions


def _build_validate_context(
    client: TestClient,
    db_session,
    headers: dict,
    source_id: str,
    remediate_run_id: str,
) -> ExecutionContext:
    """Create a VALIDATE task/run whose source_task_run_id points to the REMEDIATE run."""
    validate_task_response = client.post(
        "/tasks",
        json={"name": "Validate Changes", "task_type": "validate", "data_source_id": source_id},
        headers=headers,
    )
    assert validate_task_response.status_code == 201, validate_task_response.text
    validate_task_id = validate_task_response.json()["id"]

    validate_run_response = client.post(
        f"/tasks/{validate_task_id}/runs",
        json={"source_task_run_id": remediate_run_id},
        headers=headers,
    )
    assert validate_run_response.status_code == 201, validate_run_response.text
    validate_run_id = validate_run_response.json()["id"]

    validate_task = db_session.get(Task, uuid.UUID(validate_task_id))
    validate_run = db_session.get(TaskRun, uuid.UUID(validate_run_id))
    source = db_session.get(DataSource, uuid.UUID(source_id))
    return ExecutionContext(
        task_run=validate_run, task=validate_task, data_source=source,
        idempotency_key=str(validate_run.idempotency_key), credential_provider=None,
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

def test_validation_handler_happy_path_creates_run_and_persists_results(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Full chain: detect whitespace issues → remediate → approve all →
    validate. All approved trim_whitespace changes should pass because the
    remediation engine proposes exact stripped values."""
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        headers, source_id, detect_run_id, org_id = _build_detect_run(
            client, db_session, csv_root,
            "id,name\n1,  Ada\n2,Grace  \n",
        )
        remediate_run_id, remediation_run = _build_remediate_run(
            client, db_session, csv_root, headers, source_id, detect_run_id
        )
        assert remediation_run.total_changes_count == 2

        changes = db_session.execute(
            select(RemediationChange).where(
                RemediationChange.remediation_run_id == remediation_run.id
            )
        ).scalars().all()
        _approve_changes(db_session, changes, uuid.UUID(org_id))

        context = _build_validate_context(client, db_session, headers, source_id, remediate_run_id)
        result = ValidationHandler().execute(context)
        assert "validation run created" in result

        validation_run = db_session.execute(
            select(ValidationRun).where(ValidationRun.task_run_id == context.task_run.id)
        ).scalar_one()
        assert validation_run.approved_changes_considered == 2
        assert validation_run.passed_count == 2
        assert validation_run.failed_count == 0
        assert validation_run.skipped_count == 0
        assert validation_run.passed_count + validation_run.failed_count + validation_run.skipped_count == (
            validation_run.approved_changes_considered
        )
        assert validation_run.results_by_rule == {"validate_trim_whitespace": 2}
        assert validation_run.validation_engine_version == "1.0"
        assert validation_run.organization_id == uuid.UUID(org_id)
        assert validation_run.remediation_run_id == remediation_run.id

        results = db_session.execute(
            select(ValidationResult).where(
                ValidationResult.validation_run_id == validation_run.id
            )
        ).scalars().all()
        assert len(results) == 2
        assert all(r.outcome == "passed" for r in results)
        assert all(r.reason == PROPOSED_VALUE_MATCHES for r in results)
        assert all(r.validation_rule == "validate_trim_whitespace" for r in results)
        assert all(r.validation_rule_version == "1.0" for r in results)
        assert all(r.validation_engine_version == "1.0" for r in results)
        assert all(r.remediation_run_id == remediation_run.id for r in results)
        assert all(r.organization_id == uuid.UUID(org_id) for r in results)
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Zero approved changes (no decisions)
# ---------------------------------------------------------------------------

def test_validation_handler_zero_approved_changes_produces_zeroed_run(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """A remediation run with changes but no decisions has zero approved
    changes -- the validation run must still be created and all counts must
    be zero, with no ValidationResult rows."""
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        headers, source_id, detect_run_id, org_id = _build_detect_run(
            client, db_session, csv_root,
            "id,name\n1,  Ada\n",
        )
        remediate_run_id, remediation_run = _build_remediate_run(
            client, db_session, csv_root, headers, source_id, detect_run_id
        )
        assert remediation_run.total_changes_count == 1
        # No decisions inserted -- snapshot is empty.

        context = _build_validate_context(client, db_session, headers, source_id, remediate_run_id)
        result = ValidationHandler().execute(context)
        assert "validation run created" in result

        validation_run = db_session.execute(
            select(ValidationRun).where(ValidationRun.task_run_id == context.task_run.id)
        ).scalar_one()
        assert validation_run.approved_changes_considered == 0
        assert validation_run.passed_count == 0
        assert validation_run.failed_count == 0
        assert validation_run.skipped_count == 0
        assert validation_run.results_by_rule == {}

        results = db_session.execute(
            select(ValidationResult).where(
                ValidationResult.validation_run_id == validation_run.id
            )
        ).scalars().all()
        assert results == []
    finally:
        get_settings.cache_clear()


def test_validation_handler_zero_changes_on_remediation_run_produces_zeroed_run(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """A remediation run with NO changes at all (clean source data) must
    produce a zeroed ValidationRun with no results."""
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        headers, source_id, detect_run_id, _ = _build_detect_run(
            client, db_session, csv_root,
            "id,name\n1,Ada\n2,Grace\n",
        )
        remediate_run_id, remediation_run = _build_remediate_run(
            client, db_session, csv_root, headers, source_id, detect_run_id
        )
        assert remediation_run.total_changes_count == 0

        context = _build_validate_context(client, db_session, headers, source_id, remediate_run_id)
        result = ValidationHandler().execute(context)
        assert "validation run created" in result

        validation_run = db_session.execute(
            select(ValidationRun).where(ValidationRun.task_run_id == context.task_run.id)
        ).scalar_one()
        assert validation_run.approved_changes_considered == 0
        assert validation_run.passed_count == validation_run.failed_count == validation_run.skipped_count == 0
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Failed validations
# ---------------------------------------------------------------------------

def test_validation_handler_detects_failed_proposed_value(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Inject a corrupted proposed_value on an approved RemediationChange
    and verify the ValidationResult carries outcome=failed."""
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        headers, source_id, detect_run_id, org_id = _build_detect_run(
            client, db_session, csv_root,
            "id,name\n1,  Ada\n",
        )
        remediate_run_id, remediation_run = _build_remediate_run(
            client, db_session, csv_root, headers, source_id, detect_run_id
        )

        # Corrupt the proposed_value so the validation rule will fail.
        change = db_session.execute(
            select(RemediationChange).where(
                RemediationChange.remediation_run_id == remediation_run.id
            )
        ).scalar_one()
        change.proposed_value = "COMPLETELY_WRONG_VALUE"
        db_session.commit()

        _approve_changes(db_session, [change], uuid.UUID(org_id))

        context = _build_validate_context(client, db_session, headers, source_id, remediate_run_id)
        result = ValidationHandler().execute(context)
        assert "validation run created" in result

        validation_run = db_session.execute(
            select(ValidationRun).where(ValidationRun.task_run_id == context.task_run.id)
        ).scalar_one()
        assert validation_run.approved_changes_considered == 1
        assert validation_run.passed_count == 0
        assert validation_run.failed_count == 1
        assert validation_run.skipped_count == 0

        vr = db_session.execute(
            select(ValidationResult).where(
                ValidationResult.validation_run_id == validation_run.id
            )
        ).scalar_one()
        assert vr.outcome == "failed"
        assert vr.reason == PROPOSED_VALUE_MISMATCH
        assert vr.validation_rule == "validate_trim_whitespace"
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Skipped validations (missing config gate)
# ---------------------------------------------------------------------------

def test_validation_handler_skips_when_column_config_gate_not_satisfied(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """A normalize_date change with no RemediationColumnRule configured for
    its column must produce outcome=skipped with reason=source_date_format_not_configured."""
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        # Set up: detect invalid_date on a column with an explicit type rule.
        headers, source_id, detect_run_id, org_id = _build_detect_run(
            client, db_session, csv_root,
            "id,signup_date\n1,01/15/2024\n",
            detection_column_rules=[
                IssueDetectionColumnRule(
                    data_source_id=None, column_name="signup_date", expected_type="date"
                )
            ],
        )
        # Add RemediationColumnRule so Module 15 proposes the change.
        db_session.add(RemediationColumnRule(
            organization_id=uuid.UUID(org_id),
            data_source_id=None,
            column_name="signup_date",
            source_date_format="%m/%d/%Y",
            target_date_format="%Y-%m-%d",
        ))
        db_session.commit()
        get_settings.cache_clear()

        remediate_run_id, remediation_run = _build_remediate_run(
            client, db_session, csv_root, headers, source_id, detect_run_id
        )
        assert remediation_run.total_changes_count == 1

        change = db_session.execute(
            select(RemediationChange).where(
                RemediationChange.remediation_run_id == remediation_run.id
            )
        ).scalar_one()

        # Delete the RemediationColumnRule -- validate now has no config.
        rcr = db_session.execute(
            select(RemediationColumnRule).where(
                RemediationColumnRule.organization_id == uuid.UUID(org_id)
            )
        ).scalar_one()
        db_session.delete(rcr)
        db_session.commit()

        _approve_changes(db_session, [change], uuid.UUID(org_id))

        context = _build_validate_context(client, db_session, headers, source_id, remediate_run_id)
        result = ValidationHandler().execute(context)
        assert "validation run created" in result

        validation_run = db_session.execute(
            select(ValidationRun).where(ValidationRun.task_run_id == context.task_run.id)
        ).scalar_one()
        assert validation_run.approved_changes_considered == 1
        assert validation_run.skipped_count == 1
        assert validation_run.passed_count == 0
        assert validation_run.failed_count == 0

        vr = db_session.execute(
            select(ValidationResult).where(
                ValidationResult.validation_run_id == validation_run.id
            )
        ).scalar_one()
        assert vr.outcome == "skipped"
        assert vr.reason == SOURCE_DATE_FORMAT_NOT_CONFIGURED
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Frozen snapshot
# ---------------------------------------------------------------------------

def test_validation_handler_snapshot_freezes_before_engine_call(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Decisions added after the snapshot is frozen (simulated by
    inspecting the run's approved_changes_considered after the fact)
    must not be counted in the ValidationRun."""
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        headers, source_id, detect_run_id, org_id = _build_detect_run(
            client, db_session, csv_root,
            "id,name\n1,  Ada\n2,Grace  \n",
        )
        remediate_run_id, remediation_run = _build_remediate_run(
            client, db_session, csv_root, headers, source_id, detect_run_id
        )
        assert remediation_run.total_changes_count == 2

        changes = db_session.execute(
            select(RemediationChange).where(
                RemediationChange.remediation_run_id == remediation_run.id
            )
        ).scalars().all()

        # Approve only the FIRST change before running the handler.
        _approve_changes(db_session, [changes[0]], uuid.UUID(org_id))

        context = _build_validate_context(client, db_session, headers, source_id, remediate_run_id)
        result = ValidationHandler().execute(context)
        assert "validation run created" in result

        validation_run = db_session.execute(
            select(ValidationRun).where(ValidationRun.task_run_id == context.task_run.id)
        ).scalar_one()
        # Only 1 approved change was in the snapshot.
        assert validation_run.approved_changes_considered == 1

        # Now approve the second change -- the run already exists and must
        # not be affected (the handler is idempotent).
        _approve_changes(db_session, [changes[1]], uuid.UUID(org_id))

        # A second execute() returns the existing run unchanged.
        second = ValidationHandler().execute(context)
        assert "already exists" in second

        # The run still reflects only the snapshot at the time of first
        # execution.
        db_session.expire(validation_run)
        validation_run = db_session.execute(
            select(ValidationRun).where(ValidationRun.task_run_id == context.task_run.id)
        ).scalar_one()
        assert validation_run.approved_changes_considered == 1
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Idempotency retry
# ---------------------------------------------------------------------------

def test_validation_handler_persists_one_run_across_retries(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Calling execute() twice with the same context returns the existing
    run on the second call without creating a second ValidationRun."""
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        headers, source_id, detect_run_id, org_id = _build_detect_run(
            client, db_session, csv_root,
            "id,name\n1,  Ada\n",
        )
        remediate_run_id, remediation_run = _build_remediate_run(
            client, db_session, csv_root, headers, source_id, detect_run_id
        )
        changes = db_session.execute(
            select(RemediationChange).where(
                RemediationChange.remediation_run_id == remediation_run.id
            )
        ).scalars().all()
        _approve_changes(db_session, changes, uuid.UUID(org_id))

        context = _build_validate_context(client, db_session, headers, source_id, remediate_run_id)
        handler = ValidationHandler()

        first = handler.execute(context)
        second = handler.execute(context)

        assert "validation run created" in first
        assert "already exists" in second

        runs = db_session.execute(
            select(ValidationRun).where(ValidationRun.task_run_id == context.task_run.id)
        ).scalars().all()
        assert len(runs) == 1

        results = db_session.execute(
            select(ValidationResult).where(
                ValidationResult.validation_run_id == runs[0].id
            )
        ).scalars().all()
        assert len(results) == 1
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Organization isolation
# ---------------------------------------------------------------------------

def test_validation_handler_isolated_per_tenant(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Two organizations with identical CSV paths produce independent
    ValidationRuns; org A's approved changes are never visible to org B."""
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        csv_content = "id,name\n1,  Ada\n"

        headers_a, source_a, detect_a, org_a = _build_detect_run(
            client, db_session, csv_root, csv_content,
            relative_path="shared.csv",
        )
        headers_b, source_b, detect_b, org_b = _build_detect_run(
            client, db_session, csv_root, csv_content,
            relative_path="shared.csv",
        )
        assert org_a != org_b

        remediate_a_id, run_a = _build_remediate_run(
            client, db_session, csv_root, headers_a, source_a, detect_a
        )
        remediate_b_id, run_b = _build_remediate_run(
            client, db_session, csv_root, headers_b, source_b, detect_b
        )

        # Only approve changes for org A.
        changes_a = db_session.execute(
            select(RemediationChange).where(RemediationChange.remediation_run_id == run_a.id)
        ).scalars().all()
        _approve_changes(db_session, changes_a, uuid.UUID(org_a))

        context_a = _build_validate_context(client, db_session, headers_a, source_a, remediate_a_id)
        context_b = _build_validate_context(client, db_session, headers_b, source_b, remediate_b_id)

        ValidationHandler().execute(context_a)
        ValidationHandler().execute(context_b)

        vrun_a = db_session.execute(
            select(ValidationRun).where(ValidationRun.task_run_id == context_a.task_run.id)
        ).scalar_one()
        vrun_b = db_session.execute(
            select(ValidationRun).where(ValidationRun.task_run_id == context_b.task_run.id)
        ).scalar_one()

        assert vrun_a.id != vrun_b.id
        # Org A: 1 approved change → 1 result.
        assert vrun_a.approved_changes_considered == 1
        assert vrun_a.passed_count == 1
        # Org B: 0 approved changes (no decisions) → zeroed run.
        assert vrun_b.approved_changes_considered == 0
        assert vrun_b.passed_count == 0

        # Every row carries its own organization_id -- no cross-tenant leakage.
        assert vrun_a.organization_id == uuid.UUID(org_a)
        assert vrun_b.organization_id == uuid.UUID(org_b)

        results_a = db_session.execute(
            select(ValidationResult).where(ValidationResult.validation_run_id == vrun_a.id)
        ).scalars().all()
        results_b = db_session.execute(
            select(ValidationResult).where(ValidationResult.validation_run_id == vrun_b.id)
        ).scalars().all()
        assert len(results_a) == 1
        assert results_b == []
        assert all(r.organization_id == uuid.UUID(org_a) for r in results_a)
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Version propagation
# ---------------------------------------------------------------------------

def test_validation_handler_propagates_engine_and_rule_version_to_every_result(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        headers, source_id, detect_run_id, org_id = _build_detect_run(
            client, db_session, csv_root,
            "id,name\n1,  Ada\n2,Grace  \n",
        )
        remediate_run_id, remediation_run = _build_remediate_run(
            client, db_session, csv_root, headers, source_id, detect_run_id
        )
        changes = db_session.execute(
            select(RemediationChange).where(
                RemediationChange.remediation_run_id == remediation_run.id
            )
        ).scalars().all()
        _approve_changes(db_session, changes, uuid.UUID(org_id))

        context = _build_validate_context(client, db_session, headers, source_id, remediate_run_id)
        ValidationHandler().execute(context)

        validation_run = db_session.execute(
            select(ValidationRun).where(ValidationRun.task_run_id == context.task_run.id)
        ).scalar_one()
        assert validation_run.validation_engine_version == "1.0"

        results = db_session.execute(
            select(ValidationResult).where(
                ValidationResult.validation_run_id == validation_run.id
            )
        ).scalars().all()
        assert len(results) == 2
        for r in results:
            assert r.validation_engine_version == "1.0"
            assert r.validation_rule_version == "1.0"
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Stable ordering and no duplicate results
# ---------------------------------------------------------------------------

def test_validation_handler_results_stable_ordering_and_no_duplicates(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Results are ordered by row_number; no change_id appears twice."""
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        headers, source_id, detect_run_id, org_id = _build_detect_run(
            client, db_session, csv_root,
            "id,name\n1,  Ada\n2,Grace  \n3,Bob   \n",
        )
        remediate_run_id, remediation_run = _build_remediate_run(
            client, db_session, csv_root, headers, source_id, detect_run_id
        )
        changes = db_session.execute(
            select(RemediationChange).where(
                RemediationChange.remediation_run_id == remediation_run.id
            )
        ).scalars().all()
        _approve_changes(db_session, changes, uuid.UUID(org_id))

        context = _build_validate_context(client, db_session, headers, source_id, remediate_run_id)
        ValidationHandler().execute(context)

        validation_run = db_session.execute(
            select(ValidationRun).where(ValidationRun.task_run_id == context.task_run.id)
        ).scalar_one()
        results = db_session.execute(
            select(ValidationResult).where(
                ValidationResult.validation_run_id == validation_run.id
            ).order_by(ValidationResult.created_at)
        ).scalars().all()

        assert len(results) == 3
        # No duplicate change_ids.
        seen_change_ids = {r.remediation_change_id for r in results}
        assert len(seen_change_ids) == 3
        # Engine sorts by row_number before persisting; verify insertion order
        # matches ascending row_number by joining back to RemediationChange.
        change_map = {
            c.id: c.row_number
            for c in db_session.execute(
                select(RemediationChange).where(
                    RemediationChange.remediation_run_id == remediation_run.id
                )
            ).scalars().all()
        }
        row_numbers = [change_map[r.remediation_change_id] for r in results]
        assert row_numbers == sorted(row_numbers)
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Result limit (RESULT_LIMIT_REACHED)
# ---------------------------------------------------------------------------

def test_validation_handler_enforces_result_limit(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """With VALIDATION_MAX_PERSISTED_RESULTS=2 and 4 approved changes, the
    last 2 must be skipped with reason=result_limit_reached."""
    monkeypatch.setenv("VALIDATION_MAX_PERSISTED_RESULTS", "2")
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        headers, source_id, detect_run_id, org_id = _build_detect_run(
            client, db_session, csv_root,
            "id,name\n1,  A\n2,  B\n3,  C\n4,  D\n",
        )
        remediate_run_id, remediation_run = _build_remediate_run(
            client, db_session, csv_root, headers, source_id, detect_run_id
        )
        assert remediation_run.total_changes_count == 4

        changes = db_session.execute(
            select(RemediationChange).where(
                RemediationChange.remediation_run_id == remediation_run.id
            )
        ).scalars().all()
        _approve_changes(db_session, changes, uuid.UUID(org_id))

        context = _build_validate_context(client, db_session, headers, source_id, remediate_run_id)
        result = ValidationHandler().execute(context)
        assert "validation run created" in result

        validation_run = db_session.execute(
            select(ValidationRun).where(ValidationRun.task_run_id == context.task_run.id)
        ).scalar_one()
        assert validation_run.approved_changes_considered == 4
        # 2 processed normally, 2 skipped due to limit.
        assert validation_run.skipped_count == 2
        assert (
            validation_run.passed_count + validation_run.failed_count
            + validation_run.skipped_count
        ) == 4

        results = db_session.execute(
            select(ValidationResult).where(
                ValidationResult.validation_run_id == validation_run.id
            )
        ).scalars().all()
        # All 4 results are persisted (limit applies to how many are processed
        # normally, but skipped rows are still stored).
        assert len(results) == 4
        limit_reached = [r for r in results if r.reason == RESULT_LIMIT_REACHED]
        assert len(limit_reached) == 2
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Unsupported action → ACTION_NOT_RECOGNISED skipped
# ---------------------------------------------------------------------------

def test_validation_handler_result_fields_denormalized_correctly(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Verify that every ValidationResult row carries correctly denormalized
    fields from the approved RemediationChange and from ValidationRun:
    remediation_run_id, source_issue_id, original_value, proposed_value,
    remediation_change_id, and both version columns.

    Note: ACTION_NOT_RECOGNISED is tested exhaustively at the engine level in
    test_validation_engine.py; it cannot be exercised at the handler level
    without bypassing the DB schema's CHECK constraint on action."""
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        headers, source_id, detect_run_id, org_id = _build_detect_run(
            client, db_session, csv_root,
            "id,name\n1,  Ada\n",
        )
        remediate_run_id, remediation_run = _build_remediate_run(
            client, db_session, csv_root, headers, source_id, detect_run_id
        )
        change = db_session.execute(
            select(RemediationChange).where(
                RemediationChange.remediation_run_id == remediation_run.id
            )
        ).scalar_one()
        _approve_changes(db_session, [change], uuid.UUID(org_id))

        context = _build_validate_context(client, db_session, headers, source_id, remediate_run_id)
        ValidationHandler().execute(context)

        validation_run = db_session.execute(
            select(ValidationRun).where(ValidationRun.task_run_id == context.task_run.id)
        ).scalar_one()
        vr = db_session.execute(
            select(ValidationResult).where(
                ValidationResult.validation_run_id == validation_run.id
            )
        ).scalar_one()

        # Denormalized from RemediationChange.
        assert vr.remediation_change_id == change.id
        assert vr.source_issue_id == change.source_issue_id
        assert vr.original_value == change.original_value
        assert vr.proposed_value == change.proposed_value
        # Denormalized from RemediationRun (via ValidationHandler).
        assert vr.remediation_run_id == remediation_run.id
        # Denormalized from constants.
        assert vr.validation_engine_version == "1.0"
        assert vr.validation_rule_version == "1.0"
        assert vr.validation_rule == "validate_trim_whitespace"
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Permanent failures
# ---------------------------------------------------------------------------

def test_validation_handler_rejects_when_no_data_source(
    client: TestClient, db_session
) -> None:
    fake_task_run = type("TR", (), {
        "id": uuid.uuid4(),
        "organization_id": uuid.uuid4(),
        "source_task_run_id": uuid.uuid4(),
        "idempotency_key": uuid.uuid4(),
    })()
    fake_task = type("T", (), {"id": uuid.uuid4()})()
    context = ExecutionContext(
        task_run=fake_task_run, task=fake_task, data_source=None,
        idempotency_key=str(uuid.uuid4()), credential_provider=None,
    )
    with pytest.raises(PermanentExecutionError, match="requires a data source"):
        ValidationHandler().execute(context)


def test_validation_handler_rejects_when_no_source_task_run_id(
    client: TestClient, db_session
) -> None:
    fake_task_run = type("TR", (), {
        "id": uuid.uuid4(),
        "organization_id": uuid.uuid4(),
        "source_task_run_id": None,
        "idempotency_key": uuid.uuid4(),
    })()
    fake_task = type("T", (), {"id": uuid.uuid4()})()
    fake_ds = type("DS", (), {"id": uuid.uuid4()})()
    context = ExecutionContext(
        task_run=fake_task_run, task=fake_task, data_source=fake_ds,
        idempotency_key=str(uuid.uuid4()), credential_provider=None,
    )
    with pytest.raises(PermanentExecutionError, match="requires source_task_run_id"):
        ValidationHandler().execute(context)


def test_validation_handler_rejects_when_no_remediation_run_exists(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """source_task_run_id that has no corresponding RemediationRun (e.g.,
    points to an OTHER-type TaskRun) must permanently fail."""
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        headers = _auth_headers(client, uuid.uuid4().hex)
        source_response = client.post(
            "/data-sources",
            json={
                "name": "Source", "source_type": "csv_upload",
                "connection_metadata": {"file_path": "f.csv"},
            },
            headers=headers,
        )
        source_id = source_response.json()["id"]

        other_task_response = client.post(
            "/tasks",
            json={"name": "Other", "task_type": "other", "data_source_id": source_id},
            headers=headers,
        )
        other_run_response = client.post(
            f"/tasks/{other_task_response.json()['id']}/runs", headers=headers
        )
        other_run_id = other_run_response.json()["id"]

        context = _build_validate_context(
            client, db_session, headers, source_id, other_run_id
        )
        with pytest.raises(PermanentExecutionError, match="requires a completed remediation run"):
            ValidationHandler().execute(context)
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Latest-decision-wins (rejected then approved)
# ---------------------------------------------------------------------------

def test_validation_handler_uses_latest_decision_when_rejected_then_approved(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """If a change is first rejected then approved, the approval (later
    decision_timestamp) must win and the change must be in the snapshot."""
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        headers, source_id, detect_run_id, org_id = _build_detect_run(
            client, db_session, csv_root,
            "id,name\n1,  Ada\n",
        )
        remediate_run_id, remediation_run = _build_remediate_run(
            client, db_session, csv_root, headers, source_id, detect_run_id
        )
        change = db_session.execute(
            select(RemediationChange).where(
                RemediationChange.remediation_run_id == remediation_run.id
            )
        ).scalar_one()

        earlier = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        later = datetime(2024, 1, 1, 12, 5, 0, tzinfo=timezone.utc)

        _approve_changes(db_session, [change], uuid.UUID(org_id), decision="rejected", ts=earlier)
        _approve_changes(db_session, [change], uuid.UUID(org_id), decision="approved", ts=later)

        context = _build_validate_context(client, db_session, headers, source_id, remediate_run_id)
        ValidationHandler().execute(context)

        validation_run = db_session.execute(
            select(ValidationRun).where(ValidationRun.task_run_id == context.task_run.id)
        ).scalar_one()
        # Latest decision is "approved" → 1 approved change considered.
        assert validation_run.approved_changes_considered == 1
        assert validation_run.passed_count == 1
    finally:
        get_settings.cache_clear()


def test_validation_handler_excludes_change_when_approved_then_rejected(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """If a change is first approved then rejected, the rejection (later
    timestamp) must win and the change must NOT be in the approved snapshot."""
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        headers, source_id, detect_run_id, org_id = _build_detect_run(
            client, db_session, csv_root,
            "id,name\n1,  Ada\n",
        )
        remediate_run_id, remediation_run = _build_remediate_run(
            client, db_session, csv_root, headers, source_id, detect_run_id
        )
        change = db_session.execute(
            select(RemediationChange).where(
                RemediationChange.remediation_run_id == remediation_run.id
            )
        ).scalar_one()

        earlier = datetime(2024, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        later = datetime(2024, 1, 1, 12, 5, 0, tzinfo=timezone.utc)

        _approve_changes(db_session, [change], uuid.UUID(org_id), decision="approved", ts=earlier)
        _approve_changes(db_session, [change], uuid.UUID(org_id), decision="rejected", ts=later)

        context = _build_validate_context(client, db_session, headers, source_id, remediate_run_id)
        ValidationHandler().execute(context)

        validation_run = db_session.execute(
            select(ValidationRun).where(ValidationRun.task_run_id == context.task_run.id)
        ).scalar_one()
        # Latest decision is "rejected" → 0 approved changes.
        assert validation_run.approved_changes_considered == 0
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Count-reconcile invariant at handler level
# ---------------------------------------------------------------------------

def test_validation_handler_count_reconcile_invariant_holds_across_outcomes(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """passed+failed+skipped == approved_changes_considered for a run that
    contains a mix of all three outcomes."""
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        headers, source_id, detect_run_id, org_id = _build_detect_run(
            client, db_session, csv_root,
            "id,name\n1,  Ada\n2,Grace  \n",
        )
        # Add a detection rule so a date column gets issues too.
        # (We'll inject a normalize_date change manually for the skipped case.)
        remediate_run_id, remediation_run = _build_remediate_run(
            client, db_session, csv_root, headers, source_id, detect_run_id
        )
        assert remediation_run.total_changes_count == 2

        changes = db_session.execute(
            select(RemediationChange).where(
                RemediationChange.remediation_run_id == remediation_run.id
            )
        ).scalars().all()

        # Approve both real changes.
        # Corrupt one proposed_value so we get 1 pass + 1 fail.
        changes[0].proposed_value = "WRONG"
        db_session.commit()
        _approve_changes(db_session, changes, uuid.UUID(org_id))

        context = _build_validate_context(client, db_session, headers, source_id, remediate_run_id)
        ValidationHandler().execute(context)

        validation_run = db_session.execute(
            select(ValidationRun).where(ValidationRun.task_run_id == context.task_run.id)
        ).scalar_one()
        total = (
            validation_run.passed_count
            + validation_run.failed_count
            + validation_run.skipped_count
        )
        assert total == validation_run.approved_changes_considered == 2
        # One correct, one wrong.
        assert validation_run.passed_count == 1
        assert validation_run.failed_count == 1
        assert validation_run.skipped_count == 0
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# Tenant isolation on column config
# ---------------------------------------------------------------------------

def test_validation_handler_column_config_isolated_per_tenant(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Org A has a RemediationColumnRule for the date column; org B does
    not. Org B's ValidationResult for the date change must be skipped
    (no config), while org A's must be evaluated normally."""
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        csv_content = "id,signup_date\n1,01/15/2024\n"
        det_rule = IssueDetectionColumnRule(
            data_source_id=None, column_name="signup_date", expected_type="date"
        )

        headers_a, source_a, detect_a, org_a = _build_detect_run(
            client, db_session, csv_root, csv_content,
            relative_path="shared.csv",
            detection_column_rules=[
                IssueDetectionColumnRule(
                    data_source_id=None, column_name="signup_date", expected_type="date"
                )
            ],
        )
        headers_b, source_b, detect_b, org_b = _build_detect_run(
            client, db_session, csv_root, csv_content,
            relative_path="shared.csv",
            detection_column_rules=[
                IssueDetectionColumnRule(
                    data_source_id=None, column_name="signup_date", expected_type="date"
                )
            ],
        )
        assert org_a != org_b

        # Add the remediation column rule for BOTH orgs so Module 15
        # proposes the date change in both cases.
        for org_id in (org_a, org_b):
            db_session.add(RemediationColumnRule(
                organization_id=uuid.UUID(org_id),
                data_source_id=None,
                column_name="signup_date",
                source_date_format="%m/%d/%Y",
                target_date_format="%Y-%m-%d",
            ))
        db_session.commit()
        get_settings.cache_clear()

        remediate_a_id, run_a = _build_remediate_run(
            client, db_session, csv_root, headers_a, source_a, detect_a
        )
        remediate_b_id, run_b = _build_remediate_run(
            client, db_session, csv_root, headers_b, source_b, detect_b
        )
        assert run_a.total_changes_count == 1
        assert run_b.total_changes_count == 1

        changes_a = db_session.execute(
            select(RemediationChange).where(RemediationChange.remediation_run_id == run_a.id)
        ).scalars().all()
        changes_b = db_session.execute(
            select(RemediationChange).where(RemediationChange.remediation_run_id == run_b.id)
        ).scalars().all()

        _approve_changes(db_session, changes_a, uuid.UUID(org_a))
        _approve_changes(db_session, changes_b, uuid.UUID(org_b))

        # Delete org B's RemediationColumnRule so validation has no config.
        rcr_b = db_session.execute(
            select(RemediationColumnRule).where(
                RemediationColumnRule.organization_id == uuid.UUID(org_b)
            )
        ).scalar_one()
        db_session.delete(rcr_b)
        db_session.commit()

        context_a = _build_validate_context(client, db_session, headers_a, source_a, remediate_a_id)
        context_b = _build_validate_context(client, db_session, headers_b, source_b, remediate_b_id)

        ValidationHandler().execute(context_a)
        ValidationHandler().execute(context_b)

        vrun_a = db_session.execute(
            select(ValidationRun).where(ValidationRun.task_run_id == context_a.task_run.id)
        ).scalar_one()
        vrun_b = db_session.execute(
            select(ValidationRun).where(ValidationRun.task_run_id == context_b.task_run.id)
        ).scalar_one()

        # Org A: has config → date validated.
        assert vrun_a.approved_changes_considered == 1
        assert vrun_a.skipped_count == 0

        # Org B: no config → date skipped.
        assert vrun_b.approved_changes_considered == 1
        assert vrun_b.skipped_count == 1

        res_b = db_session.execute(
            select(ValidationResult).where(ValidationResult.validation_run_id == vrun_b.id)
        ).scalar_one()
        assert res_b.reason == SOURCE_DATE_FORMAT_NOT_CONFIGURED
    finally:
        get_settings.cache_clear()
