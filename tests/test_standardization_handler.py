"""Module 7 integration tests for StandardizationHandler: full execution
against a real fixture file, all the way through the genuine
SYNC -> profile -> TRANSFORM -> clean -> approve -> STANDARDIZE pipeline
(the same "build a real upstream result via the real handlers" discipline
test_cleaning_handler.py established for Module 6), including idempotency
across retries, the source-CleaningRun-status gate (must be approved),
tenant isolation on output file placement, an explicit hash-unchanged
proof for the Module 6 output being standardized, and the added Module 7
idempotency acceptance criterion (standardizing an already-standardized
result produces zero further changes)."""
import hashlib
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.models.cleaning_run import CleaningRun
from app.models.data_source import DataSource
from app.models.standardization_change import StandardizationChange
from app.models.standardization_column_mapping import StandardizationColumnMapping
from app.models.standardization_lookup_entry import StandardizationLookupEntry
from app.models.standardization_run import StandardizationRun
from app.models.task import Task
from app.models.task_run import TaskRun
from app.worker.handlers.base import ExecutionContext, PermanentExecutionError
from app.worker.handlers.cleaning import CleaningHandler
from app.worker.handlers.csv_profiling import CsvProfilingHandler
from app.worker.handlers.standardization import StandardizationHandler

CSV_CONTENT = (
    "id,name,email\n"
    "1,  jane doe  ,Jane@Example.com\n"
    "2,bob smith,BOB@EXAMPLE.COM\n"
)


def _auth_headers(client: TestClient, suffix: str) -> dict:
    response = client.post(
        "/auth/register",
        json={
            "organization_name": f"Standardization Org {suffix}",
            "email": f"standardization-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Standardization User",
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _set_roots(monkeypatch, tmp_path: Path) -> tuple[Path, Path, Path]:
    csv_root = tmp_path / "csv_in"
    csv_root.mkdir()
    output_root = tmp_path / "csv_out"
    standardized_root = tmp_path / "csv_standardized"
    monkeypatch.setenv("CSV_INPUT_ROOT", str(csv_root))
    monkeypatch.setenv("CSV_OUTPUT_ROOT", str(output_root))
    monkeypatch.setenv("CSV_STANDARDIZED_ROOT", str(standardized_root))
    get_settings.cache_clear()
    return csv_root, output_root, standardized_root


def _build_approved_cleaning_run(
    client: TestClient,
    db_session,
    csv_root: Path,
    *,
    relative_path: str = "customers.csv",
    csv_content: str = CSV_CONTENT,
    approve: bool = True,
    suffix: str | None = None,
) -> tuple[ExecutionContext, str]:
    """Full real pipeline through CsvProfilingHandler and CleaningHandler
    (identical to test_cleaning_handler.py's _make_pipeline), then
    optionally approves the resulting CleaningRun via the real API.
    Returns (transform_context, organization_id_str). The cleaning run's
    sync_run_id is stashed on the returned context as
    transform_context.task_run.source_task_run_id for the caller to build
    a STANDARDIZE run against."""
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

    sync_task_response = client.post(
        "/tasks",
        json={"name": "Sync Customers", "task_type": "sync", "data_source_id": source_id},
        headers=headers,
    )
    assert sync_task_response.status_code == 201, sync_task_response.text
    sync_run_response = client.post(
        f"/tasks/{sync_task_response.json()['id']}/runs", headers=headers
    )
    assert sync_run_response.status_code == 201, sync_run_response.text
    sync_run_id = sync_run_response.json()["id"]

    org_dir = csv_root / organization_id
    org_dir.mkdir(parents=True, exist_ok=True)
    (org_dir / relative_path).write_text(csv_content, encoding="utf-8")

    sync_task = db_session.get(Task, uuid.UUID(sync_task_response.json()["id"]))
    sync_run = db_session.get(TaskRun, uuid.UUID(sync_run_id))
    source = db_session.get(DataSource, uuid.UUID(source_id))
    CsvProfilingHandler().execute(
        ExecutionContext(
            task_run=sync_run,
            task=sync_task,
            data_source=source,
            idempotency_key=str(sync_run.idempotency_key),
            credential_provider=None,
        )
    )

    transform_task_response = client.post(
        "/tasks",
        json={"name": "Clean Customers", "task_type": "transform", "data_source_id": source_id},
        headers=headers,
    )
    assert transform_task_response.status_code == 201, transform_task_response.text
    transform_run_response = client.post(
        f"/tasks/{transform_task_response.json()['id']}/runs",
        json={"source_task_run_id": sync_run_id},
        headers=headers,
    )
    assert transform_run_response.status_code == 201, transform_run_response.text
    transform_task_id = transform_task_response.json()["id"]
    transform_run_id = transform_run_response.json()["id"]

    transform_task = db_session.get(Task, uuid.UUID(transform_task_id))
    transform_run = db_session.get(TaskRun, uuid.UUID(transform_run_id))
    CleaningHandler().execute(
        ExecutionContext(
            task_run=transform_run,
            task=transform_task,
            data_source=source,
            idempotency_key=str(transform_run.idempotency_key),
            credential_provider=None,
        )
    )

    if approve:
        approve_response = client.post(
            f"/tasks/{transform_task_id}/runs/{transform_run_id}/cleaning/approve",
            headers=headers,
        )
        assert approve_response.status_code == 200, approve_response.text

    return headers, source_id, transform_run_id, organization_id


def _build_standardize_context(
    client: TestClient,
    db_session,
    headers: dict,
    source_id: str,
    transform_run_id: str,
) -> ExecutionContext:
    standardize_task_response = client.post(
        "/tasks",
        json={
            "name": "Standardize Customers",
            "task_type": "standardize",
            "data_source_id": source_id,
        },
        headers=headers,
    )
    assert standardize_task_response.status_code == 201, standardize_task_response.text
    standardize_task_id = standardize_task_response.json()["id"]

    standardize_run_response = client.post(
        f"/tasks/{standardize_task_id}/runs",
        json={"source_task_run_id": transform_run_id},
        headers=headers,
    )
    assert standardize_run_response.status_code == 201, standardize_run_response.text
    standardize_run_id = standardize_run_response.json()["id"]

    standardize_task = db_session.get(Task, uuid.UUID(standardize_task_id))
    standardize_run = db_session.get(TaskRun, uuid.UUID(standardize_run_id))
    source = db_session.get(DataSource, uuid.UUID(source_id))
    return ExecutionContext(
        task_run=standardize_run,
        task=standardize_task,
        data_source=source,
        idempotency_key=str(standardize_run.idempotency_key),
        credential_provider=None,
    )


def test_standardization_handler_persists_one_run_across_retries(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root, _, _ = _set_roots(monkeypatch, tmp_path)
    try:
        headers, source_id, transform_run_id, _ = _build_approved_cleaning_run(
            client, db_session, csv_root
        )
        context = _build_standardize_context(client, db_session, headers, source_id, transform_run_id)
        handler = StandardizationHandler()

        first = handler.execute(context)
        second = handler.execute(context)

        runs = db_session.execute(
            select(StandardizationRun).where(StandardizationRun.task_run_id == context.task_run.id)
        ).scalars().all()
        assert len(runs) == 1
        assert "standardization run created" in first
        assert "already exists" in second
        assert runs[0].status == "pending_review"
        assert runs[0].standardization_engine_version == "1.0"
    finally:
        get_settings.cache_clear()


def test_standardization_handler_never_writes_to_the_cleaning_output_file(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Automated proof (Acceptance Criteria item 3): the Module 6 output
    file being standardized is byte-identical before and after."""
    csv_root, _, _ = _set_roots(monkeypatch, tmp_path)
    try:
        headers, source_id, transform_run_id, _ = _build_approved_cleaning_run(
            client, db_session, csv_root
        )
        cleaning_run = db_session.execute(
            select(CleaningRun).where(CleaningRun.task_run_id == uuid.UUID(transform_run_id))
        ).scalar_one()
        source_path = Path(cleaning_run.output_file_path)
        before_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()

        context = _build_standardize_context(client, db_session, headers, source_id, transform_run_id)
        StandardizationHandler().execute(context)

        after_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        assert before_hash == after_hash
    finally:
        get_settings.cache_clear()


def test_standardization_handler_writes_output_under_its_own_tenant_scoped_root(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root, output_root, standardized_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers, source_id, transform_run_id, org_id = _build_approved_cleaning_run(
            client, db_session, csv_root
        )
        context = _build_standardize_context(client, db_session, headers, source_id, transform_run_id)
        StandardizationHandler().execute(context)

        standardization_run = db_session.execute(
            select(StandardizationRun).where(StandardizationRun.task_run_id == context.task_run.id)
        ).scalar_one()
        output_path = Path(standardization_run.output_file_path)
        expected_org_dir = standardized_root / org_id
        assert output_path.is_relative_to(expected_org_dir)
        assert output_path.is_file()
        # Never written under either the Module 5 input root or the
        # Module 6 output root -- a third, distinct location.
        assert not output_path.is_relative_to(csv_root)
        assert not output_path.is_relative_to(output_root)
    finally:
        get_settings.cache_clear()


def test_standardization_handler_records_bounded_changes_and_accurate_aggregate(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root, _, _ = _set_roots(monkeypatch, tmp_path)
    try:
        headers, source_id, transform_run_id, _ = _build_approved_cleaning_run(
            client, db_session, csv_root
        )
        context = _build_standardize_context(client, db_session, headers, source_id, transform_run_id)
        StandardizationHandler().execute(context)

        standardization_run = db_session.execute(
            select(StandardizationRun).where(StandardizationRun.task_run_id == context.task_run.id)
        ).scalar_one()
        persisted_changes = db_session.execute(
            select(StandardizationChange).where(
                StandardizationChange.standardization_run_id == standardization_run.id
            )
        ).scalars().all()

        assert standardization_run.total_changes_count > 0
        assert len(persisted_changes) == standardization_run.total_changes_count
        for change in persisted_changes:
            assert change.field_type
            assert change.rule_name
            assert change.reason
            assert change.rule_version == "1.0"
            assert 0.0 < change.confidence_score <= 1.0
    finally:
        get_settings.cache_clear()


def test_standardization_handler_rejects_when_no_cleaning_run_exists_for_source_run(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root, _, _ = _set_roots(monkeypatch, tmp_path)
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

        # An unrelated SYNC run that was never cleaned.
        sync_task_response = client.post(
            "/tasks",
            json={"name": "Sync", "task_type": "sync", "data_source_id": source_id},
            headers=headers,
        )
        sync_run_response = client.post(
            f"/tasks/{sync_task_response.json()['id']}/runs", headers=headers
        )
        sync_run_id = sync_run_response.json()["id"]

        context = _build_standardize_context(client, db_session, headers, source_id, sync_run_id)

        with pytest.raises(PermanentExecutionError, match="requires a completed cleaning run"):
            StandardizationHandler().execute(context)
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("terminal_action", ["reject", None])
def test_standardization_handler_rejects_unapproved_cleaning_run(
    client: TestClient, db_session, tmp_path: Path, monkeypatch, terminal_action: str | None
) -> None:
    """StandardizationHandler must refuse to run against a CleaningRun
    that is not status=approved -- verified for pending_review (no action
    taken) and rejected here; rolled_back is covered by the dedicated test
    below (it requires first approving, which changes the fixture shape)."""
    csv_root, _, _ = _set_roots(monkeypatch, tmp_path)
    try:
        headers, source_id, transform_run_id, _ = _build_approved_cleaning_run(
            client, db_session, csv_root, approve=False
        )
        transform_run = db_session.execute(
            select(TaskRun).where(TaskRun.id == uuid.UUID(transform_run_id))
        ).scalar_one()
        transform_task_id = str(transform_run.task_id)

        if terminal_action == "reject":
            reject_response = client.post(
                f"/tasks/{transform_task_id}/runs/{transform_run_id}/cleaning/reject",
                headers=headers,
            )
            assert reject_response.status_code == 200, reject_response.text
            expected_status = "rejected"
        else:
            expected_status = "pending_review"

        context = _build_standardize_context(client, db_session, headers, source_id, transform_run_id)

        with pytest.raises(PermanentExecutionError, match=f"current status: {expected_status}"):
            StandardizationHandler().execute(context)
    finally:
        get_settings.cache_clear()


def test_standardization_handler_rejects_rolled_back_cleaning_run(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root, _, _ = _set_roots(monkeypatch, tmp_path)
    try:
        headers, source_id, transform_run_id, _ = _build_approved_cleaning_run(
            client, db_session, csv_root, approve=True
        )
        transform_run = db_session.execute(
            select(TaskRun).where(TaskRun.id == uuid.UUID(transform_run_id))
        ).scalar_one()
        transform_task_id = str(transform_run.task_id)

        rollback_response = client.post(
            f"/tasks/{transform_task_id}/runs/{transform_run_id}/cleaning/rollback",
            headers=headers,
        )
        assert rollback_response.status_code == 200, rollback_response.text

        context = _build_standardize_context(client, db_session, headers, source_id, transform_run_id)

        with pytest.raises(PermanentExecutionError, match="current status: rolled_back"):
            StandardizationHandler().execute(context)
    finally:
        get_settings.cache_clear()


def test_standardization_handler_output_isolated_per_tenant_even_with_identical_relative_paths(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """B1-equivalent proof for Module 7: two different orgs standardizing
    a source at the identical relative path must never collide."""
    csv_root, _, _ = _set_roots(monkeypatch, tmp_path)
    try:
        headers_a, source_a, run_a, org_a = _build_approved_cleaning_run(
            client, db_session, csv_root, relative_path="shared.csv"
        )
        headers_b, source_b, run_b, org_b = _build_approved_cleaning_run(
            client, db_session, csv_root, relative_path="shared.csv"
        )
        assert org_a != org_b

        context_a = _build_standardize_context(client, db_session, headers_a, source_a, run_a)
        context_b = _build_standardize_context(client, db_session, headers_b, source_b, run_b)
        StandardizationHandler().execute(context_a)
        StandardizationHandler().execute(context_b)

        std_a = db_session.execute(
            select(StandardizationRun).where(StandardizationRun.task_run_id == context_a.task_run.id)
        ).scalar_one()
        std_b = db_session.execute(
            select(StandardizationRun).where(StandardizationRun.task_run_id == context_b.task_run.id)
        ).scalar_one()
        assert std_a.id != std_b.id
        assert Path(std_a.output_file_path).parent != Path(std_b.output_file_path).parent
    finally:
        get_settings.cache_clear()


def test_standardization_handler_consults_org_column_mapping_override(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """A column the built-in heuristic would leave unclassified is
    standardized once an organization-configured mapping declares its
    field_type -- proving StandardizationColumnMapping is actually wired
    into the handler, not just modeled."""
    csv_root, _, _ = _set_roots(monkeypatch, tmp_path)
    try:
        headers, source_id, transform_run_id, org_id = _build_approved_cleaning_run(
            client,
            db_session,
            csv_root,
            csv_content="id,contact_value\n1,  jane doe  \n",
        )
        # "contact_value" matches no built-in header pattern.
        mapping_response = client.post(
            "/tasks/standardization/column-mappings",
            json={"column_name": "contact_value", "field_type": "person_name"},
            headers=headers,
        )
        assert mapping_response.status_code == 201, mapping_response.text

        context = _build_standardize_context(client, db_session, headers, source_id, transform_run_id)
        StandardizationHandler().execute(context)

        standardization_run = db_session.execute(
            select(StandardizationRun).where(StandardizationRun.task_run_id == context.task_run.id)
        ).scalar_one()
        changes = db_session.execute(
            select(StandardizationChange).where(
                StandardizationChange.standardization_run_id == standardization_run.id
            )
        ).scalars().all()
        assert any(c.field_type == "person_name" and c.column_name == "contact_value" for c in changes)
    finally:
        get_settings.cache_clear()


def test_standardization_handler_consults_org_lookup_entry_override(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """An organization-configured lookup entry (company suffix
    canonicalization) is actually applied -- proving
    StandardizationLookupEntry is wired in, not just modeled."""
    csv_root, _, _ = _set_roots(monkeypatch, tmp_path)
    try:
        headers, source_id, transform_run_id, org_id = _build_approved_cleaning_run(
            client,
            db_session,
            csv_root,
            csv_content="id,company\n1,Acme Inc\n",
        )
        lookup_response = client.post(
            "/tasks/standardization/lookup-entries",
            json={
                "field_type": "company_name",
                "lookup_key": "acme inc",
                "lookup_value": "Acme Incorporated",
            },
            headers=headers,
        )
        assert lookup_response.status_code == 201, lookup_response.text

        context = _build_standardize_context(client, db_session, headers, source_id, transform_run_id)
        StandardizationHandler().execute(context)

        standardization_run = db_session.execute(
            select(StandardizationRun).where(StandardizationRun.task_run_id == context.task_run.id)
        ).scalar_one()
        changes = db_session.execute(
            select(StandardizationChange).where(
                StandardizationChange.standardization_run_id == standardization_run.id
            )
        ).scalars().all()
        assert any(c.standardized_value == "Acme Incorporated" for c in changes)
    finally:
        get_settings.cache_clear()


def test_standardization_handler_second_run_over_own_output_is_idempotent(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """The added Module 7 acceptance criterion (design doc Section 11):
    Clean -> Standardize -> Standardize again produces zero additional
    changes, and the second run's output is byte-identical to the first's.
    Exercised here at the full-pipeline level: the first standardization
    run's own output is fed back through a second, independent
    SYNC -> TRANSFORM(clean, no-op) -> STANDARDIZE pipeline."""
    csv_root, output_root, standardized_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers, source_id, transform_run_id, org_id = _build_approved_cleaning_run(
            client, db_session, csv_root
        )
        context = _build_standardize_context(client, db_session, headers, source_id, transform_run_id)
        StandardizationHandler().execute(context)
        first_run = db_session.execute(
            select(StandardizationRun).where(StandardizationRun.task_run_id == context.task_run.id)
        ).scalar_one()
        assert first_run.total_changes_count > 0

        # Second independent pipeline: clean (no-op, already standardized
        # values pass through unchanged) then standardize the first run's
        # own standardized output.
        second_headers, second_source_id, second_transform_run_id, _ = _build_approved_cleaning_run(
            client,
            db_session,
            csv_root,
            csv_content=Path(first_run.output_file_path).read_text(encoding="utf-8"),
            suffix=uuid.uuid4().hex,
        )
        second_context = _build_standardize_context(
            client, db_session, second_headers, second_source_id, second_transform_run_id
        )
        StandardizationHandler().execute(second_context)

        second_run = db_session.execute(
            select(StandardizationRun).where(
                StandardizationRun.task_run_id == second_context.task_run.id
            )
        ).scalar_one()
        assert second_run.total_changes_count == 0
        assert second_run.confidence_score == 1.0
        assert (
            Path(second_run.output_file_path).read_bytes()
            == Path(first_run.output_file_path).read_bytes()
        )
    finally:
        get_settings.cache_clear()
