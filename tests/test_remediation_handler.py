"""Module 15 Phase 3 RemediationHandler integration tests. Mirrors
test_issue_detection_handler.py's fixture conventions for the upstream
DETECT stage (RemediationHandler consumes an IssueDetectionRun directly,
no SYNC/TRANSFORM/STANDARDIZE stage is required first), and
test_matching_handler.py / test_standardization_handler.py's conventions
for idempotency, tenant isolation, and configuration-driven behavior.

Covers every category the Module 15 kickoff message named for Phase 3:
happy path, hash mismatch, missing IssueDetectionRun, missing
configuration, empty issue set, duplicate retry/idempotency, cross-tenant
isolation, and the persisted-change limit. Full regression suite and
migration verification are run separately, not in this file."""
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
from app.models.remediation_change import RemediationChange
from app.models.remediation_column_rule import RemediationColumnRule
from app.models.remediation_dataset_config import RemediationDatasetConfig
from app.models.remediation_run import RemediationRun
from app.models.task import Task
from app.models.task_run import TaskRun
from app.worker.handlers.base import ExecutionContext, PermanentExecutionError
from app.worker.handlers.issue_detection import IssueDetectionHandler
from app.worker.handlers.remediation import RemediationHandler


def _auth_headers(client: TestClient, suffix: str) -> dict:
    response = client.post(
        "/auth/register",
        json={
            "organization_name": f"Remediation Org {suffix}",
            "email": f"remediate-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Remediation User",
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


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
    """Creates a data source + DETECT task/run and executes the real
    IssueDetectionHandler against it (no SYNC/profiling stage needed --
    IssueDetectionHandler reads the raw CSV directly, same as Module 14's
    own handler tests). Returns (headers, source_id, detect_run_id,
    organization_id). detection_column_rules, if given, are persisted
    (with organization_id filled in) before DETECT runs, for tests that
    need a Module-14-side gate (e.g. expected_type="date") satisfied
    before an Issue can even exist for Module 15 to remediate."""
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


def _build_remediate_context(
    client: TestClient,
    db_session,
    headers: dict,
    source_id: str,
    source_task_run_id: str,
) -> ExecutionContext:
    remediate_task_response = client.post(
        "/tasks",
        json={"name": "Remediate Issues", "task_type": "remediate", "data_source_id": source_id},
        headers=headers,
    )
    assert remediate_task_response.status_code == 201, remediate_task_response.text
    remediate_task_id = remediate_task_response.json()["id"]

    remediate_run_response = client.post(
        f"/tasks/{remediate_task_id}/runs",
        json={"source_task_run_id": source_task_run_id},
        headers=headers,
    )
    assert remediate_run_response.status_code == 201, remediate_run_response.text
    remediate_run_id = remediate_run_response.json()["id"]

    remediate_task = db_session.get(Task, uuid.UUID(remediate_task_id))
    remediate_run = db_session.get(TaskRun, uuid.UUID(remediate_run_id))
    source = db_session.get(DataSource, uuid.UUID(source_id))
    return ExecutionContext(
        task_run=remediate_run, task=remediate_task, data_source=source,
        idempotency_key=str(remediate_run.idempotency_key), credential_provider=None,
    )


def _set_csv_root(monkeypatch, tmp_path: Path) -> Path:
    csv_root = tmp_path / "csv"
    csv_root.mkdir()
    monkeypatch.setenv("CSV_INPUT_ROOT", str(csv_root))
    get_settings.cache_clear()
    return csv_root


# --- happy path ---------------------------------------------------------------


def test_remediation_handler_happy_path_creates_run_and_persists_changes(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        headers, source_id, detect_run_id, org_id = _build_detect_run(
            client, db_session, csv_root,
            "id,name\n1,  Ada\n2,Grace  \n",
        )
        context = _build_remediate_context(client, db_session, headers, source_id, detect_run_id)

        result = RemediationHandler().execute(context)
        assert "remediation run created" in result

        remediation_run = db_session.execute(
            select(RemediationRun).where(RemediationRun.task_run_id == context.task_run.id)
        ).scalar_one()
        assert remediation_run.issues_considered_count == 2
        assert remediation_run.total_changes_count == 2
        assert remediation_run.issues_skipped_count == 0
        assert remediation_run.changes_by_action == {"trim_whitespace": 2}
        assert remediation_run.remediation_engine_version == "1.0"
        assert remediation_run.organization_id == uuid.UUID(org_id)

        changes = db_session.execute(
            select(RemediationChange).where(
                RemediationChange.remediation_run_id == remediation_run.id
            )
        ).scalars().all()
        assert len(changes) == 2
        assert {c.action for c in changes} == {"trim_whitespace"}
        assert {c.proposed_value for c in changes} == {"Ada", "Grace"}
        assert all(c.confidence == 1.0 for c in changes)
        assert all(c.source_issue_id is not None for c in changes)
    finally:
        get_settings.cache_clear()


# --- duplicate retry / idempotency ---------------------------------------------


def test_remediation_handler_persists_one_run_across_retries(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        headers, source_id, detect_run_id, _ = _build_detect_run(
            client, db_session, csv_root, "id,name\n1,  Ada\n",
        )
        context = _build_remediate_context(client, db_session, headers, source_id, detect_run_id)
        handler = RemediationHandler()

        first = handler.execute(context)
        second = handler.execute(context)

        runs = db_session.execute(
            select(RemediationRun).where(RemediationRun.task_run_id == context.task_run.id)
        ).scalars().all()
        assert len(runs) == 1
        assert "remediation run created" in first
        assert "already exists" in second

        changes = db_session.execute(
            select(RemediationChange).where(RemediationChange.remediation_run_id == runs[0].id)
        ).scalars().all()
        assert len(changes) == 1
    finally:
        get_settings.cache_clear()


# --- hash mismatch fails safely -------------------------------------------------


def test_remediation_handler_rejects_when_source_file_changed_since_detection(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        headers, source_id, detect_run_id, org_id = _build_detect_run(
            client, db_session, csv_root, "id,name\n1,  Ada\n",
        )
        # Mutate the source file after detection but before remediation --
        # still a valid CSV, just different content/hash.
        (csv_root / org_id / "customers.csv").write_text(
            "id,name\n1,  Ada\n2,  Grace\n", encoding="utf-8"
        )
        context = _build_remediate_context(client, db_session, headers, source_id, detect_run_id)

        with pytest.raises(PermanentExecutionError, match="source data has changed"):
            RemediationHandler().execute(context)

        # No RemediationRun/RemediationChange must exist after a failed,
        # safely-rejected attempt.
        assert (
            db_session.execute(
                select(RemediationRun).where(RemediationRun.task_run_id == context.task_run.id)
            ).scalar_one_or_none()
            is None
        )
    finally:
        get_settings.cache_clear()


# --- missing IssueDetectionRun --------------------------------------------------


def test_remediation_handler_rejects_when_no_detection_run_exists_for_source_run(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
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

        # An OTHER-type task/run exists in the same org, but no
        # IssueDetectionRun was ever created for it.
        other_task_response = client.post(
            "/tasks",
            json={"name": "Something Else", "task_type": "other", "data_source_id": source_id},
            headers=headers,
        )
        other_run_response = client.post(
            f"/tasks/{other_task_response.json()['id']}/runs", headers=headers
        )
        other_run_id = other_run_response.json()["id"]

        context = _build_remediate_context(client, db_session, headers, source_id, other_run_id)

        with pytest.raises(
            PermanentExecutionError, match="requires a completed issue detection run"
        ):
            RemediationHandler().execute(context)
    finally:
        get_settings.cache_clear()


def test_remediation_handler_rejects_detection_run_that_predates_hash_tracking(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        headers, source_id, detect_run_id, _ = _build_detect_run(
            client, db_session, csv_root, "id,name\n1,  Ada\n",
        )
        detection_run = db_session.execute(
            select(IssueDetectionRun).where(
                IssueDetectionRun.task_run_id == uuid.UUID(detect_run_id)
            )
        ).scalar_one()
        detection_run.source_sha256 = None
        db_session.commit()

        context = _build_remediate_context(client, db_session, headers, source_id, detect_run_id)

        with pytest.raises(
            PermanentExecutionError, match="predates source hash tracking"
        ):
            RemediationHandler().execute(context)
    finally:
        get_settings.cache_clear()


# --- missing configuration is a documented skip, never an error ----------------


def test_remediation_handler_completes_with_documented_skip_when_config_missing(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """duplicate_row is detected unconditionally by Module 14 (no
    IssueDetectionColumnRule needed), but Module 15 never proposes its
    removal without an explicit, active RemediationDatasetConfig -- with
    none configured here, the run must still complete successfully, with
    the issue recorded as skipped, never raising."""
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        headers, source_id, detect_run_id, _ = _build_detect_run(
            client, db_session, csv_root,
            "id,name\n1,Bob Jones\n1,Bob Jones\n",
        )
        context = _build_remediate_context(client, db_session, headers, source_id, detect_run_id)

        result = RemediationHandler().execute(context)
        assert "remediation run created" in result

        remediation_run = db_session.execute(
            select(RemediationRun).where(RemediationRun.task_run_id == context.task_run.id)
        ).scalar_one()
        assert remediation_run.issues_considered_count == 1
        assert remediation_run.total_changes_count == 0
        assert remediation_run.issues_skipped_count == 1
        assert remediation_run.changes_by_action == {}
    finally:
        get_settings.cache_clear()


# --- empty issue set -------------------------------------------------------------


def test_remediation_handler_zero_issues_produces_a_zeroed_run(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        headers, source_id, detect_run_id, _ = _build_detect_run(
            client, db_session, csv_root, "id,name\n1,Ada\n2,Grace\n",
        )
        detection_run = db_session.execute(
            select(IssueDetectionRun).where(
                IssueDetectionRun.task_run_id == uuid.UUID(detect_run_id)
            )
        ).scalar_one()
        assert detection_run.total_issues_found == 0

        context = _build_remediate_context(client, db_session, headers, source_id, detect_run_id)
        result = RemediationHandler().execute(context)
        assert "remediation run created" in result

        remediation_run = db_session.execute(
            select(RemediationRun).where(RemediationRun.task_run_id == context.task_run.id)
        ).scalar_one()
        assert remediation_run.issues_considered_count == 0
        assert remediation_run.total_changes_count == 0
        assert remediation_run.issues_skipped_count == 0
        assert remediation_run.changes_by_action == {}

        changes = db_session.execute(
            select(RemediationChange).where(
                RemediationChange.remediation_run_id == remediation_run.id
            )
        ).scalars().all()
        assert changes == []
    finally:
        get_settings.cache_clear()


# --- persisted change limit -------------------------------------------------------


def test_remediation_handler_enforces_persisted_change_limit(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("REMEDIATION_MAX_PERSISTED_CHANGES", "2")
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        headers, source_id, detect_run_id, _ = _build_detect_run(
            client, db_session, csv_root,
            "id,name\n1,  A\n2,  B\n3,  C\n4,  D\n",
        )
        context = _build_remediate_context(client, db_session, headers, source_id, detect_run_id)

        result = RemediationHandler().execute(context)
        assert "remediation run created" in result

        remediation_run = db_session.execute(
            select(RemediationRun).where(RemediationRun.task_run_id == context.task_run.id)
        ).scalar_one()
        assert remediation_run.issues_considered_count == 4
        assert remediation_run.total_changes_count == 2
        assert remediation_run.issues_skipped_count == 2
        assert remediation_run.total_changes_count + remediation_run.issues_skipped_count == (
            remediation_run.issues_considered_count
        )

        changes = db_session.execute(
            select(RemediationChange).where(
                RemediationChange.remediation_run_id == remediation_run.id
            )
        ).scalars().all()
        assert len(changes) == 2
    finally:
        get_settings.cache_clear()


# --- tenant isolation --------------------------------------------------------------


def test_remediation_handler_isolated_per_tenant_with_identical_paths_and_divergent_config(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Two different organizations remediate a source at the identical
    relative path, with genuinely different RemediationColumnRule
    configuration -- same isolation proof
    test_standardization_handler_output_isolated_per_tenant_even_with_
    identical_relative_paths already establishes for Module 7, extended
    here to also prove config resolution itself never leaks across
    organization_id."""
    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        csv_content = "id,signup_date\n1,01/15/2024\n"
        # Module 14 only flags invalid_date on a column with an explicit
        # expected_type="date" IssueDetectionColumnRule -- both orgs need
        # this for the Issue to exist at all; only org A additionally gets
        # the Module 15 RemediationColumnRule needed to remediate it.
        headers_a, source_a, detect_a, org_a = _build_detect_run(
            client, db_session, csv_root, csv_content, relative_path="shared.csv",
            detection_column_rules=[
                IssueDetectionColumnRule(
                    data_source_id=None, column_name="signup_date", expected_type="date"
                )
            ],
        )
        headers_b, source_b, detect_b, org_b = _build_detect_run(
            client, db_session, csv_root, csv_content, relative_path="shared.csv",
            detection_column_rules=[
                IssueDetectionColumnRule(
                    data_source_id=None, column_name="signup_date", expected_type="date"
                )
            ],
        )
        assert org_a != org_b

        # Org A configures date remediation; org B configures nothing at
        # all for the same column name.
        db_session.add(
            RemediationColumnRule(
                organization_id=uuid.UUID(org_a),
                data_source_id=None,
                column_name="signup_date",
                source_date_format="%m/%d/%Y",
                target_date_format="%Y-%m-%d",
            )
        )
        db_session.commit()

        context_a = _build_remediate_context(client, db_session, headers_a, source_a, detect_a)
        context_b = _build_remediate_context(client, db_session, headers_b, source_b, detect_b)
        RemediationHandler().execute(context_a)
        RemediationHandler().execute(context_b)

        run_a = db_session.execute(
            select(RemediationRun).where(RemediationRun.task_run_id == context_a.task_run.id)
        ).scalar_one()
        run_b = db_session.execute(
            select(RemediationRun).where(RemediationRun.task_run_id == context_b.task_run.id)
        ).scalar_one()
        assert run_a.id != run_b.id

        # Org A's configuration produced a real proposal; org B's
        # identical-shaped issue, with no configuration of its own, was
        # correctly skipped -- proving RemediationColumnRule resolution is
        # scoped per organization_id, never inherited across tenants.
        assert run_a.total_changes_count == 1
        assert run_b.total_changes_count == 0
        assert run_b.issues_skipped_count == 1

        changes_a = db_session.execute(
            select(RemediationChange).where(RemediationChange.remediation_run_id == run_a.id)
        ).scalars().all()
        changes_b = db_session.execute(
            select(RemediationChange).where(RemediationChange.remediation_run_id == run_b.id)
        ).scalars().all()
        assert len(changes_a) == 1
        assert changes_a[0].proposed_value == "2024-01-15"
        assert changes_b == []

        # Org-scoped queries never cross: every change/run row carries
        # exactly its own organization_id.
        assert all(c.organization_id == uuid.UUID(org_a) for c in changes_a)
        assert run_a.organization_id != run_b.organization_id
    finally:
        get_settings.cache_clear()


def test_remediation_handler_rejects_unsupported_source_type(
    client: TestClient, db_session,
) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    source_response = client.post(
        "/data-sources",
        json={
            "name": "Postgres Source", "source_type": "postgres",
            "connection_metadata": {"host": "db"},
        },
        headers=headers,
    )
    source_id = source_response.json()["id"]

    other_task_response = client.post(
        "/tasks",
        json={"name": "Something Else", "task_type": "other", "data_source_id": source_id},
        headers=headers,
    )
    other_run_response = client.post(
        f"/tasks/{other_task_response.json()['id']}/runs", headers=headers
    )
    other_run_id = other_run_response.json()["id"]

    context = _build_remediate_context(client, db_session, headers, source_id, other_run_id)

    with pytest.raises(PermanentExecutionError, match="not implemented"):
        RemediationHandler().execute(context)


def test_remediation_handler_never_writes_to_the_source_file(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Strictly read-only proof, same hash-before/after discipline
    IssueDetectionHandler's own equivalent test uses."""
    import hashlib

    csv_root = _set_csv_root(monkeypatch, tmp_path)
    try:
        headers, source_id, detect_run_id, org_id = _build_detect_run(
            client, db_session, csv_root, "id,name\n1,  Ada\n",
        )
        source_path = csv_root / org_id / "customers.csv"
        before = hashlib.sha256(source_path.read_bytes()).hexdigest()

        context = _build_remediate_context(client, db_session, headers, source_id, detect_run_id)
        RemediationHandler().execute(context)

        after = hashlib.sha256(source_path.read_bytes()).hexdigest()
        assert before == after
    finally:
        get_settings.cache_clear()
