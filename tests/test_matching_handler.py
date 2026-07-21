"""Module 8 integration tests for MatchHandler: full execution against a
real fixture file, all the way through the genuine
SYNC -> profile -> TRANSFORM -> clean -> approve -> STANDARDIZE ->
standardize -> approve -> MATCH -> match pipeline (the same "build a real
upstream result via the real handlers" discipline
test_standardization_handler.py established for Module 7), including
idempotency across retries, the source-StandardizationRun-status gate
(must be approved), the no-output-file guarantee, an explicit
hash-unchanged proof for the Module 7 output being matched, tenant
isolation, and the skipped-block/blocking_key auditing added in the
approved design revision."""
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.core.config import get_settings
from app.models.data_source import DataSource
from app.models.match_decision import MatchDecision
from app.models.match_group import MatchGroup
from app.models.match_rule_field import MatchRuleField
from app.models.match_rule_set import MatchRuleSet
from app.models.match_run import MatchRun
from app.models.match_skipped_block import MatchSkippedBlock
from app.models.standardization_run import StandardizationRun
from app.models.task import Task
from app.models.task_run import TaskRun
from app.worker.handlers.base import ExecutionContext, PermanentExecutionError
from app.worker.handlers.cleaning import CleaningHandler
from app.worker.handlers.csv_profiling import CsvProfilingHandler
from app.worker.handlers.matching import MatchHandler
from app.worker.handlers.standardization import StandardizationHandler

CSV_CONTENT = (
    "id,name,email\n"
    "1,  jane doe  ,Jane@Example.com\n"
    "2,bob smith,BOB@EXAMPLE.COM\n"
    "3,bob smith,bob@example.com\n"
)


def _auth_headers(client: TestClient, suffix: str) -> dict:
    response = client.post(
        "/auth/register",
        json={
            "organization_name": f"Matching Org {suffix}",
            "email": f"matching-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Matching User",
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


def _build_approved_standardization_run(
    client: TestClient,
    db_session,
    csv_root: Path,
    *,
    relative_path: str = "customers.csv",
    csv_content: str = CSV_CONTENT,
    approve: bool = True,
    suffix: str | None = None,
) -> tuple[dict, str, str, str]:
    """Full real pipeline through CsvProfilingHandler, CleaningHandler, and
    StandardizationHandler, approving both the CleaningRun and (optionally)
    the StandardizationRun via the real API. Returns (headers, source_id,
    standardize_run_id, organization_id)."""
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

    if approve:
        approve_response = client.post(
            f"/tasks/{standardize_task_id}/runs/{standardize_run_id}/standardization/approve",
            headers=headers,
        )
        assert approve_response.status_code == 200, approve_response.text

    return headers, source_id, standardize_run_id, organization_id


def _build_match_context(
    client: TestClient,
    db_session,
    headers: dict,
    source_id: str,
    standardize_run_id: str,
) -> ExecutionContext:
    match_task_response = client.post(
        "/tasks",
        json={"name": "Match Customers", "task_type": "match", "data_source_id": source_id},
        headers=headers,
    )
    assert match_task_response.status_code == 201, match_task_response.text
    match_task_id = match_task_response.json()["id"]

    match_run_response = client.post(
        f"/tasks/{match_task_id}/runs",
        json={"source_task_run_id": standardize_run_id},
        headers=headers,
    )
    assert match_run_response.status_code == 201, match_run_response.text
    match_run_id = match_run_response.json()["id"]

    match_task = db_session.get(Task, uuid.UUID(match_task_id))
    match_run = db_session.get(TaskRun, uuid.UUID(match_run_id))
    source = db_session.get(DataSource, uuid.UUID(source_id))
    return ExecutionContext(
        task_run=match_run, task=match_task, data_source=source,
        idempotency_key=str(match_run.idempotency_key), credential_provider=None,
    )


def test_match_handler_persists_one_run_across_retries(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root, _, _ = _set_roots(monkeypatch, tmp_path)
    try:
        headers, source_id, standardize_run_id, _ = _build_approved_standardization_run(
            client, db_session, csv_root
        )
        context = _build_match_context(client, db_session, headers, source_id, standardize_run_id)
        handler = MatchHandler()

        first = handler.execute(context)
        second = handler.execute(context)

        runs = db_session.execute(
            select(MatchRun).where(MatchRun.task_run_id == context.task_run.id)
        ).scalars().all()
        assert len(runs) == 1
        assert "match run created" in first
        assert "already exists" in second
        assert runs[0].status == "pending_review"
        assert runs[0].match_engine_version == "1.0"
    finally:
        get_settings.cache_clear()


def test_match_handler_never_writes_to_the_standardized_output_file(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Automated proof: the Module 7 output file being matched is
    byte-identical before and after -- and no NEW file is written at all
    (Module 8 produces no output file, unlike Modules 6/7)."""
    import hashlib

    csv_root, _, _ = _set_roots(monkeypatch, tmp_path)
    try:
        headers, source_id, standardize_run_id, _ = _build_approved_standardization_run(
            client, db_session, csv_root
        )
        standardization_run = db_session.execute(
            select(StandardizationRun).where(
                StandardizationRun.task_run_id == uuid.UUID(standardize_run_id)
            )
        ).scalar_one()
        source_path = Path(standardization_run.output_file_path)
        before_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()

        files_before = {p for p in tmp_path.rglob("*") if p.is_file()}

        context = _build_match_context(client, db_session, headers, source_id, standardize_run_id)
        MatchHandler().execute(context)

        after_hash = hashlib.sha256(source_path.read_bytes()).hexdigest()
        assert before_hash == after_hash

        files_after = {p for p in tmp_path.rglob("*") if p.is_file()}
        assert files_after == files_before, "MatchHandler must write no new file at all"
    finally:
        get_settings.cache_clear()


def test_match_handler_rejects_when_no_standardization_run_exists_for_source_run(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root, _, _ = _set_roots(monkeypatch, tmp_path)
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

        sync_task_response = client.post(
            "/tasks",
            json={"name": "Sync", "task_type": "sync", "data_source_id": source_id},
            headers=headers,
        )
        sync_run_response = client.post(
            f"/tasks/{sync_task_response.json()['id']}/runs", headers=headers
        )
        sync_run_id = sync_run_response.json()["id"]

        context = _build_match_context(client, db_session, headers, source_id, sync_run_id)

        with pytest.raises(PermanentExecutionError, match="requires a completed standardization run"):
            MatchHandler().execute(context)
    finally:
        get_settings.cache_clear()


@pytest.mark.parametrize("terminal_action", ["reject", None])
def test_match_handler_rejects_unapproved_standardization_run(
    client: TestClient, db_session, tmp_path: Path, monkeypatch, terminal_action: str | None
) -> None:
    csv_root, _, _ = _set_roots(monkeypatch, tmp_path)
    try:
        headers, source_id, standardize_run_id, _ = _build_approved_standardization_run(
            client, db_session, csv_root, approve=False
        )
        standardize_run = db_session.execute(
            select(TaskRun).where(TaskRun.id == uuid.UUID(standardize_run_id))
        ).scalar_one()
        standardize_task_id = str(standardize_run.task_id)

        if terminal_action == "reject":
            reject_response = client.post(
                f"/tasks/{standardize_task_id}/runs/{standardize_run_id}/standardization/reject",
                headers=headers,
            )
            assert reject_response.status_code == 200, reject_response.text
            expected_status = "rejected"
        else:
            expected_status = "pending_review"

        context = _build_match_context(client, db_session, headers, source_id, standardize_run_id)

        with pytest.raises(PermanentExecutionError, match=f"current status: {expected_status}"):
            MatchHandler().execute(context)
    finally:
        get_settings.cache_clear()


def test_match_handler_rejects_rolled_back_standardization_run(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root, _, _ = _set_roots(monkeypatch, tmp_path)
    try:
        headers, source_id, standardize_run_id, _ = _build_approved_standardization_run(
            client, db_session, csv_root, approve=True
        )
        standardize_run = db_session.execute(
            select(TaskRun).where(TaskRun.id == uuid.UUID(standardize_run_id))
        ).scalar_one()
        standardize_task_id = str(standardize_run.task_id)

        rollback_response = client.post(
            f"/tasks/{standardize_task_id}/runs/{standardize_run_id}/standardization/rollback",
            headers=headers,
        )
        assert rollback_response.status_code == 200, rollback_response.text

        context = _build_match_context(client, db_session, headers, source_id, standardize_run_id)

        with pytest.raises(PermanentExecutionError, match="current status: rolled_back"):
            MatchHandler().execute(context)
    finally:
        get_settings.cache_clear()


def test_match_handler_finds_exact_duplicate_rows_with_no_rule_set_configured(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Two genuinely whole-row-identical standardized rows (including id)
    are caught by Stage 1 with zero configuration -- no MatchRuleSet
    needed at all."""
    csv_root, _, _ = _set_roots(monkeypatch, tmp_path)
    try:
        exact_dup_csv = (
            "id,name,email\n"
            "1,  jane doe  ,Jane@Example.com\n"
            "2,bob smith,BOB@EXAMPLE.COM\n"
            "2,bob smith,BOB@EXAMPLE.COM\n"
        )
        headers, source_id, standardize_run_id, _ = _build_approved_standardization_run(
            client, db_session, csv_root, csv_content=exact_dup_csv
        )
        context = _build_match_context(client, db_session, headers, source_id, standardize_run_id)
        MatchHandler().execute(context)

        match_run = db_session.execute(
            select(MatchRun).where(MatchRun.task_run_id == context.task_run.id)
        ).scalar_one()
        assert match_run.rule_set_id is None
        assert match_run.duplicate_group_count == 1

        groups = db_session.execute(
            select(MatchGroup).where(MatchGroup.match_run_id == match_run.id)
        ).scalars().all()
        assert len(groups) == 1
        assert groups[0].record_count == 2

        decisions = db_session.execute(
            select(MatchDecision).where(MatchDecision.match_run_id == match_run.id)
        ).scalars().all()
        assert len(decisions) == 1
        assert decisions[0].rule_name == "exact_row_match"
        assert decisions[0].blocking_key is None
    finally:
        get_settings.cache_clear()


def test_match_handler_uses_data_source_specific_rule_set_over_org_wide(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    csv_root, _, _ = _set_roots(monkeypatch, tmp_path)
    try:
        headers, source_id, standardize_run_id, _ = _build_approved_standardization_run(
            client, db_session, csv_root
        )

        org_wide = client.post(
            "/tasks/matching/rule-sets",
            json={
                "duplicate_threshold": 0.99, "review_threshold": 0.98,
                "fields": [{"column_name": "email", "comparison_type": "normalized_exact", "weight": 1.0}],
            },
            headers=headers,
        )
        assert org_wide.status_code == 201, org_wide.text

        scoped = client.post(
            "/tasks/matching/rule-sets",
            json={
                "data_source_id": source_id,
                "duplicate_threshold": 0.5, "review_threshold": 0.1,
                "fields": [{"column_name": "name", "comparison_type": "normalized_exact", "weight": 1.0}],
            },
            headers=headers,
        )
        assert scoped.status_code == 201, scoped.text
        scoped_id = scoped.json()["id"]

        context = _build_match_context(client, db_session, headers, source_id, standardize_run_id)
        MatchHandler().execute(context)

        match_run = db_session.execute(
            select(MatchRun).where(MatchRun.task_run_id == context.task_run.id)
        ).scalar_one()
        assert str(match_run.rule_set_id) == scoped_id
    finally:
        get_settings.cache_clear()


def test_match_handler_skipped_block_is_recorded_and_bounded(
    client: TestClient, db_session, tmp_path: Path, monkeypatch
) -> None:
    """Three rows share one email (blocking key) but differ in name, so
    none collapse via Stage 1 -- with MATCH_MAX_BLOCK_SIZE=1, this block
    of size 3 must be skipped and recorded, not compared."""
    csv_root, _, _ = _set_roots(monkeypatch, tmp_path)
    try:
        monkeypatch.setenv("MATCH_MAX_BLOCK_SIZE", "1")
        monkeypatch.setenv("MATCH_MAX_SKIPPED_ROW_SAMPLE", "2")
        get_settings.cache_clear()
        skip_csv = (
            "id,name,email\n"
            "1,Alice One,shared@example.com\n"
            "2,Alice Two,shared@example.com\n"
            "3,Alice Three,shared@example.com\n"
        )
        headers, source_id, standardize_run_id, _ = _build_approved_standardization_run(
            client, db_session, csv_root, csv_content=skip_csv
        )
        rs = client.post(
            "/tasks/matching/rule-sets",
            json={
                "duplicate_threshold": 0.9, "review_threshold": 0.1,
                "fields": [{"column_name": "email", "comparison_type": "normalized_exact", "weight": 1.0}],
            },
            headers=headers,
        )
        assert rs.status_code == 201, rs.text

        context = _build_match_context(client, db_session, headers, source_id, standardize_run_id)
        MatchHandler().execute(context)

        match_run = db_session.execute(
            select(MatchRun).where(MatchRun.task_run_id == context.task_run.id)
        ).scalar_one()
        skipped = db_session.execute(
            select(MatchSkippedBlock).where(MatchSkippedBlock.match_run_id == match_run.id)
        ).scalars().all()

        assert match_run.skipped_block_count == 1
        assert len(skipped) == 1
        assert skipped[0].blocking_key == "shared@example.com"
        assert skipped[0].block_size == 3
        assert skipped[0].sample_row_indices == [0, 1]

        # No decisions or groups were produced for the skipped block.
        decisions = db_session.execute(
            select(MatchDecision).where(MatchDecision.match_run_id == match_run.id)
        ).scalars().all()
        assert decisions == []
        groups = db_session.execute(
            select(MatchGroup).where(MatchGroup.match_run_id == match_run.id)
        ).scalars().all()
        assert groups == []
    finally:
        get_settings.cache_clear()
