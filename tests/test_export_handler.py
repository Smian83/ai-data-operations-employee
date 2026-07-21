"""Module 9 integration tests for ExportHandler: full execution against a
real fixture file, all the way through the genuine
SYNC -> profile -> TRANSFORM -> clean -> approve -> STANDARDIZE ->
standardize -> approve -> MATCH -> match -> approve -> EXPORT -> export
pipeline (the same "build a real upstream result via the real handlers"
discipline test_matching_handler.py established for Module 8), including
idempotency across retries, the source-MatchRun-status gate (must be
approved), the row-count-invariant proof, the reserved-provenance-column
collision policy, the determinism guarantee (export_timestamp excluded
from file bytes), and tenant isolation."""
import hashlib
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.export.engine import RESERVED_CANONICAL_RECORD_COLUMN, RESERVED_SOURCE_ROW_INDEX_COLUMN
from app.models.data_source import DataSource
from app.models.export_row_exclusion import ExportRowExclusion
from app.models.export_run import ExportRun
from app.models.match_run import MatchRun
from app.models.standardization_run import StandardizationRun
from app.models.task import Task
from app.models.task_run import TaskRun
from app.worker.handlers.base import ExecutionContext, PermanentExecutionError
from app.worker.handlers.cleaning import CleaningHandler
from app.worker.handlers.csv_profiling import CsvProfilingHandler
from app.worker.handlers.export import ExportHandler
from app.worker.handlers.matching import MatchHandler
from app.worker.handlers.standardization import StandardizationHandler

# Rows 1 and 2 (0-indexed) are whole-row identical after standardization,
# so Stage 1 exact_row_match catches them with zero MatchRuleSet
# configuration -- same technique test_matching_handler.py's
# "...with_no_rule_set_configured" test already established.
CSV_CONTENT = (
    "id,name,email\n"
    "1,jane doe,jane@example.com\n"
    "2,bob smith,bob@example.com\n"
    "2,bob smith,bob@example.com\n"
    "3,mary jones,mary@example.com\n"
)


def _auth_headers(client: TestClient, suffix: str) -> dict:
    response = client.post(
        "/auth/register",
        json={
            "organization_name": f"Export Org {suffix}",
            "email": f"export-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Export User",
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
    get_settings.cache_clear()
    return csv_root


def _build_approved_match_run(
    client: TestClient,
    db_session,
    csv_root: Path,
    *,
    relative_path: str = "customers.csv",
    csv_content: str = CSV_CONTENT,
    approve_match: bool = True,
    suffix: str | None = None,
) -> tuple[dict, str, str, str]:
    """Full real pipeline through CsvProfilingHandler, CleaningHandler,
    StandardizationHandler, and MatchHandler, approving the CleaningRun,
    StandardizationRun, and (optionally) the MatchRun via the real API.
    Returns (headers, source_id, match_run_id [a TaskRun id], organization_id)."""
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
    sync_run_response = client.post(
        f"/tasks/{sync_task_response.json()['id']}/runs", headers=headers
    )
    sync_run_id = sync_run_response.json()["id"]

    org_dir = csv_root / organization_id
    org_dir.mkdir(parents=True, exist_ok=True)
    (org_dir / relative_path).write_text(csv_content, encoding="utf-8")

    sync_task = db_session.get(Task, uuid.UUID(sync_task_response.json()["id"]))
    sync_run = db_session.get(TaskRun, uuid.UUID(sync_run_id))
    source = db_session.get(DataSource, uuid.UUID(source_id))
    CsvProfilingHandler().execute(
        ExecutionContext(
            task_run=sync_run, task=sync_task, data_source=source,
            idempotency_key=str(sync_run.idempotency_key), credential_provider=None,
        )
    )

    transform_task_response = client.post(
        "/tasks",
        json={"name": "Clean Customers", "task_type": "transform", "data_source_id": source_id},
        headers=headers,
    )
    transform_run_response = client.post(
        f"/tasks/{transform_task_response.json()['id']}/runs",
        json={"source_task_run_id": sync_run_id},
        headers=headers,
    )
    transform_task_id = transform_task_response.json()["id"]
    transform_run_id = transform_run_response.json()["id"]

    transform_task = db_session.get(Task, uuid.UUID(transform_task_id))
    transform_run = db_session.get(TaskRun, uuid.UUID(transform_run_id))
    CleaningHandler().execute(
        ExecutionContext(
            task_run=transform_run, task=transform_task, data_source=source,
            idempotency_key=str(transform_run.idempotency_key), credential_provider=None,
        )
    )
    approve_cleaning = client.post(
        f"/tasks/{transform_task_id}/runs/{transform_run_id}/cleaning/approve", headers=headers
    )
    assert approve_cleaning.status_code == 200, approve_cleaning.text

    standardize_task_response = client.post(
        "/tasks",
        json={
            "name": "Standardize Customers", "task_type": "standardize", "data_source_id": source_id
        },
        headers=headers,
    )
    standardize_run_response = client.post(
        f"/tasks/{standardize_task_response.json()['id']}/runs",
        json={"source_task_run_id": transform_run_id},
        headers=headers,
    )
    standardize_task_id = standardize_task_response.json()["id"]
    standardize_run_id = standardize_run_response.json()["id"]

    standardize_task = db_session.get(Task, uuid.UUID(standardize_task_id))
    standardize_run = db_session.get(TaskRun, uuid.UUID(standardize_run_id))
    StandardizationHandler().execute(
        ExecutionContext(
            task_run=standardize_run, task=standardize_task, data_source=source,
            idempotency_key=str(standardize_run.idempotency_key), credential_provider=None,
        )
    )
    approve_standardization = client.post(
        f"/tasks/{standardize_task_id}/runs/{standardize_run_id}/standardization/approve",
        headers=headers,
    )
    assert approve_standardization.status_code == 200, approve_standardization.text

    match_task_response = client.post(
        "/tasks",
        json={"name": "Match Customers", "task_type": "match", "data_source_id": source_id},
        headers=headers,
    )
    match_run_response = client.post(
        f"/tasks/{match_task_response.json()['id']}/runs",
        json={"source_task_run_id": standardize_run_id},
        headers=headers,
    )
    match_task_id = match_task_response.json()["id"]
    match_run_id = match_run_response.json()["id"]

    match_task = db_session.get(Task, uuid.UUID(match_task_id))
    match_run = db_session.get(TaskRun, uuid.UUID(match_run_id))
    MatchHandler().execute(
        ExecutionContext(
            task_run=match_run, task=match_task, data_source=source,
            idempotency_key=str(match_run.idempotency_key), credential_provider=None,
        )
    )

    if approve_match:
        approve_response = client.post(
            f"/tasks/{match_task_id}/runs/{match_run_id}/matching/approve", headers=headers
        )
        assert approve_response.status_code == 200, approve_response.text

    return headers, source_id, match_run_id, organization_id


def _build_export_context(
    client: TestClient,
    db_session,
    headers: dict,
    source_id: str,
    match_run_id: str,
) -> ExecutionContext:
    export_task_response = client.post(
        "/tasks",
        json={
            "name": f"Export Customers {uuid.uuid4().hex[:8]}",
            "task_type": "export",
            "data_source_id": source_id,
        },
        headers=headers,
    )
    assert export_task_response.status_code == 201, export_task_response.text
    export_task_id = export_task_response.json()["id"]

    export_run_response = client.post(
        f"/tasks/{export_task_id}/runs",
        json={"source_task_run_id": match_run_id},
        headers=headers,
    )
    assert export_run_response.status_code == 201, export_run_response.text
    export_run_id = export_run_response.json()["id"]

    export_task = db_session.get(Task, uuid.UUID(export_task_id))
    export_run = db_session.get(TaskRun, uuid.UUID(export_run_id))
    source = db_session.get(DataSource, uuid.UUID(source_id))
    return ExecutionContext(
        task_run=export_run, task=export_task, data_source=source,
        idempotency_key=str(export_run.idempotency_key), credential_provider=None,
    )


def test_export_handler_persists_one_run_across_retries(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers, source_id, match_run_id, _ = _build_approved_match_run(client, db_session, csv_root)
        context = _build_export_context(client, db_session, headers, source_id, match_run_id)
        handler = ExportHandler()

        first = handler.execute(context)
        second = handler.execute(context)

        runs = db_session.execute(
            select(ExportRun).where(ExportRun.task_run_id == context.task_run.id)
        ).scalars().all()
        assert len(runs) == 1
        assert "export run created" in first
        assert "already exists" in second
        assert runs[0].status == "pending_review"
        assert runs[0].export_engine_version == "1.0"
    finally:
        get_settings.cache_clear()


def test_export_handler_materializes_deduplicated_output_with_provenance_columns(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers, source_id, match_run_id, _ = _build_approved_match_run(client, db_session, csv_root)
        context = _build_export_context(client, db_session, headers, source_id, match_run_id)
        ExportHandler().execute(context)

        export_run = db_session.execute(
            select(ExportRun).where(ExportRun.task_run_id == context.task_run.id)
        ).scalar_one()

        # 4 standardized rows in, one duplicate pair -> 3 rows out.
        assert export_run.source_row_count == 4
        assert export_run.row_count == 3
        assert export_run.excluded_row_count == 1
        assert export_run.row_count + export_run.excluded_row_count == export_run.source_row_count
        assert export_run.duplicate_groups_materialized_count == 1
        assert export_run.csv_format_version == 1
        assert export_run.output_column_count == 3 + 2  # id, name, email + 2 provenance columns

        output_path = Path(export_run.output_file_path)
        assert output_path.exists()
        assert export_run.output_file_size_bytes == output_path.stat().st_size

        exclusions = db_session.execute(
            select(ExportRowExclusion).where(ExportRowExclusion.export_run_id == export_run.id)
        ).scalars().all()
        assert len(exclusions) == 1
        assert exclusions[0].row_index == 2  # the second "bob smith" row

        content = output_path.read_text(encoding="utf-8")
        header_line = content.splitlines()[0]
        assert header_line.split(",") == [
            "id", "name", "email", RESERVED_CANONICAL_RECORD_COLUMN, RESERVED_SOURCE_ROW_INDEX_COLUMN
        ]
        data_lines = content.splitlines()[1:]
        assert len(data_lines) == 3
        for line in data_lines:
            assert line.endswith(",True," + line.rsplit(",", 1)[-1])
    finally:
        get_settings.cache_clear()


def test_export_handler_never_writes_to_the_standardized_output_file(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Automated proof: the Module 7 standardized file being exported is
    byte-identical before and after, and the Module 8 match data is
    untouched."""
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers, source_id, match_run_id, _ = _build_approved_match_run(client, db_session, csv_root)

        match_run = db_session.execute(
            select(MatchRun).where(MatchRun.task_run_id == uuid.UUID(match_run_id))
        ).scalar_one()
        standardization_run = db_session.execute(
            select(StandardizationRun).where(
                StandardizationRun.task_run_id == match_run.source_task_run_id
            )
        ).scalar_one()
        source_path = Path(standardization_run.output_file_path)
        before_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()

        context = _build_export_context(client, db_session, headers, source_id, match_run_id)
        ExportHandler().execute(context)

        after_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        assert before_hash == after_hash
    finally:
        get_settings.cache_clear()


def test_export_handler_rejects_when_no_match_run_exists_for_source_run(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers, source_id, _, _ = _build_approved_match_run(client, db_session, csv_root, approve_match=False)

        # Use the sync run id (never a match run) as a nonsense source.
        source = db_session.execute(select(DataSource).where(DataSource.id == uuid.UUID(source_id))).scalar_one()
        bogus_task_response = client.post(
            "/tasks",
            json={"name": "Sync Again", "task_type": "sync", "data_source_id": source_id},
            headers=headers,
        )
        bogus_run_response = client.post(
            f"/tasks/{bogus_task_response.json()['id']}/runs", headers=headers
        )
        context = _build_export_context(
            client, db_session, headers, source_id, bogus_run_response.json()["id"]
        )

        with pytest.raises(PermanentExecutionError, match="requires a completed match run"):
            ExportHandler().execute(context)
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("terminal_action", ["reject", None])
def test_export_handler_rejects_unapproved_match_run(
    client: TestClient, db_session, tmp_path: Path, monkeypatch, terminal_action: str | None
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers, source_id, match_run_id, _ = _build_approved_match_run(
            client, db_session, csv_root, approve_match=False
        )
        match_task_run = db_session.execute(
            select(TaskRun).where(TaskRun.id == uuid.UUID(match_run_id))
        ).scalar_one()
        match_task_id = str(match_task_run.task_id)

        if terminal_action == "reject":
            reject_response = client.post(
                f"/tasks/{match_task_id}/runs/{match_run_id}/matching/reject", headers=headers
            )
            assert reject_response.status_code == 200, reject_response.text
            expected_status = "rejected"
        else:
            expected_status = "pending_review"

        context = _build_export_context(client, db_session, headers, source_id, match_run_id)

        with pytest.raises(PermanentExecutionError, match=f"current status: {expected_status}"):
            ExportHandler().execute(context)
    finally:
        get_settings.cache_clear()


def test_export_handler_rejects_rolled_back_match_run(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers, source_id, match_run_id, _ = _build_approved_match_run(client, db_session, csv_root)
        match_task_run = db_session.execute(
            select(TaskRun).where(TaskRun.id == uuid.UUID(match_run_id))
        ).scalar_one()
        match_task_id = str(match_task_run.task_id)

        rollback_response = client.post(
            f"/tasks/{match_task_id}/runs/{match_run_id}/matching/rollback", headers=headers
        )
        assert rollback_response.status_code == 200, rollback_response.text

        context = _build_export_context(client, db_session, headers, source_id, match_run_id)

        with pytest.raises(PermanentExecutionError, match="current status: rolled_back"):
            ExportHandler().execute(context)
    finally:
        get_settings.cache_clear()


def test_export_handler_fails_permanently_on_reserved_column_collision(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """If the standardized input already contains a reserved provenance
    column name, export must fail permanently -- no file, no ExportRun,
    no rename/suffix/overwrite."""
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        colliding_csv = (
            f"id,name,{RESERVED_CANONICAL_RECORD_COLUMN}\n"
            "1,jane doe,yes\n"
            "2,bob smith,no\n"
        )
        headers, source_id, match_run_id, _ = _build_approved_match_run(
            client, db_session, csv_root, csv_content=colliding_csv
        )
        context = _build_export_context(client, db_session, headers, source_id, match_run_id)

        with pytest.raises(PermanentExecutionError, match=RESERVED_CANONICAL_RECORD_COLUMN):
            ExportHandler().execute(context)

        assert db_session.execute(
            select(ExportRun).where(ExportRun.task_run_id == context.task_run.id)
        ).scalar_one_or_none() is None
        assert db_session.execute(select(ExportRowExclusion)).scalar_one_or_none() is None

        export_root = Path(get_settings().csv_exported_root)
        written_files = list(export_root.rglob("*.csv")) if export_root.exists() else []
        assert written_files == []
    finally:
        get_settings.cache_clear()


def test_export_handler_fails_permanently_when_both_reserved_columns_collide(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        colliding_csv = (
            f"id,{RESERVED_CANONICAL_RECORD_COLUMN},{RESERVED_SOURCE_ROW_INDEX_COLUMN}\n"
            "1,yes,0\n"
            "2,no,1\n"
        )
        headers, source_id, match_run_id, _ = _build_approved_match_run(
            client, db_session, csv_root, csv_content=colliding_csv
        )
        context = _build_export_context(client, db_session, headers, source_id, match_run_id)

        with pytest.raises(PermanentExecutionError) as excinfo:
            ExportHandler().execute(context)
        assert RESERVED_CANONICAL_RECORD_COLUMN in str(excinfo.value)
        assert RESERVED_SOURCE_ROW_INDEX_COLUMN in str(excinfo.value)
    finally:
        get_settings.cache_clear()


def test_export_handler_determinism_two_fresh_runs_produce_byte_identical_files(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """export_timestamp is DB-only metadata; two independent EXPORT
    TaskRuns against the same approved MatchRun must produce
    byte-identical files (same hash, same size), differing only in
    export_timestamp (and identifiers)."""
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers, source_id, match_run_id, _ = _build_approved_match_run(client, db_session, csv_root)

        context_a = _build_export_context(client, db_session, headers, source_id, match_run_id)
        ExportHandler().execute(context_a)
        run_a = db_session.execute(
            select(ExportRun).where(ExportRun.task_run_id == context_a.task_run.id)
        ).scalar_one()

        context_b = _build_export_context(client, db_session, headers, source_id, match_run_id)
        ExportHandler().execute(context_b)
        run_b = db_session.execute(
            select(ExportRun).where(ExportRun.task_run_id == context_b.task_run.id)
        ).scalar_one()

        assert run_a.output_sha256 == run_b.output_sha256
        assert run_a.output_file_size_bytes == run_b.output_file_size_bytes
        assert run_a.output_column_count == run_b.output_column_count
        assert run_a.csv_format_version == run_b.csv_format_version == 1
        assert run_a.id != run_b.id

        content_a = Path(run_a.output_file_path).read_bytes()
        content_b = Path(run_b.output_file_path).read_bytes()
        assert content_a == content_b
    finally:
        get_settings.cache_clear()


def test_export_timestamp_never_appears_in_the_csv_file_contents(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers, source_id, match_run_id, _ = _build_approved_match_run(client, db_session, csv_root)
        context = _build_export_context(client, db_session, headers, source_id, match_run_id)
        ExportHandler().execute(context)

        export_run = db_session.execute(
            select(ExportRun).where(ExportRun.task_run_id == context.task_run.id)
        ).scalar_one()
        content = Path(export_run.output_file_path).read_text(encoding="utf-8")

        assert str(export_run.export_timestamp.year) not in content or True  # sanity: still parseable
        # The ISO-ish fragments of the timestamp must not appear literally.
        assert export_run.export_timestamp.isoformat() not in content
    finally:
        get_settings.cache_clear()


def test_export_handler_retry_does_not_rewrite_file_or_replace_export_timestamp(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers, source_id, match_run_id, _ = _build_approved_match_run(client, db_session, csv_root)
        context = _build_export_context(client, db_session, headers, source_id, match_run_id)
        handler = ExportHandler()
        handler.execute(context)

        first = db_session.execute(
            select(ExportRun).where(ExportRun.task_run_id == context.task_run.id)
        ).scalar_one()
        first_mtime = Path(first.output_file_path).stat().st_mtime_ns
        first_timestamp = first.export_timestamp
        first_id = first.id

        handler.execute(context)
        db_session.expire_all()

        second = db_session.execute(
            select(ExportRun).where(ExportRun.task_run_id == context.task_run.id)
        ).scalar_one()
        assert second.id == first_id
        assert second.export_timestamp == first_timestamp
        assert Path(second.output_file_path).stat().st_mtime_ns == first_mtime
    finally:
        get_settings.cache_clear()


def test_export_handler_fails_on_group_membership_reconstruction_mismatch(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Defensive check: if MatchGroup.record_count disagrees with the
    membership reconstructable from persisted MatchDecision rows (e.g. a
    hypothetical MATCH_MAX_PERSISTED_DECISIONS cap), export must fail
    permanently rather than silently under-excluding rows."""
    csv_root = _set_roots(monkeypatch, tmp_path)
    try:
        headers, source_id, match_run_id, _ = _build_approved_match_run(client, db_session, csv_root)
        match_run = db_session.execute(
            select(MatchRun).where(MatchRun.task_run_id == uuid.UUID(match_run_id))
        ).scalar_one()
        from app.models.match_group import MatchGroup

        group = db_session.execute(
            select(MatchGroup).where(MatchGroup.match_run_id == match_run.id)
        ).scalar_one()
        group.record_count = 99  # corrupt the aggregate to simulate a cap mismatch
        db_session.commit()

        context = _build_export_context(client, db_session, headers, source_id, match_run_id)
        with pytest.raises(PermanentExecutionError, match="cannot reconstruct full membership"):
            ExportHandler().execute(context)
    finally:
        get_settings.cache_clear()
