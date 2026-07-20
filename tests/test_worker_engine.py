"""Tests for the Module 4 execution engine: atomic claiming, lease_token
fencing (heartbeat/completion must present the current token), retry/backoff
with idempotency_key stability, and the DB-level lease-consistency CHECK
constraint."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.models.enums import TaskRunStatus
from app.models.task import Task
from app.models.task_run import TaskRun
from app.models.task_run_event import TaskRunEvent
from app.worker.engine import (
    LeaseLostError,
    claim_batch,
    complete_failure,
    complete_success,
    heartbeat,
)


def _register(client: TestClient, org_name: str, email: str) -> dict:
    resp = client.post(
        "/auth/register",
        json={
            "organization_name": org_name,
            "email": email,
            "password": "correct-horse-battery",
            "full_name": "Test User",
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _auth_headers(client: TestClient, org_name: str, email: str) -> dict:
    token = _register(client, org_name, email)["access_token"]
    return {"Authorization": f"Bearer {token}"}


def _make_task_run(client: TestClient, db_session, org_name: str, email: str, **task_overrides) -> TaskRun:
    headers = _auth_headers(client, org_name, email)
    payload = {"name": "Nightly Sync", "task_type": "sync"}
    payload.update(task_overrides)
    task_resp = client.post("/tasks", json=payload, headers=headers)
    assert task_resp.status_code == 201, task_resp.text
    run_resp = client.post(f"/tasks/{task_resp.json()['id']}/runs", headers=headers)
    assert run_resp.status_code == 201, run_resp.text
    return db_session.get(TaskRun, uuid.UUID(run_resp.json()["id"]))


# --- Claiming ----------------------------------------------------------------


def test_claim_batch_transitions_pending_to_running(client: TestClient, db_session) -> None:
    run = _make_task_run(client, db_session, "Org Engine A", "engine-a@example.com")
    claimed = claim_batch(db_session, worker_id="w1")
    assert [r.id for r in claimed] == [run.id]

    db_session.refresh(run)
    assert run.status == TaskRunStatus.RUNNING
    assert run.lease_token is not None
    assert run.lease_expires_at is not None
    assert run.started_at is not None
    assert run.attempt_count == 1


def test_claim_batch_does_not_reclaim_running_rows(client: TestClient, db_session) -> None:
    _make_task_run(client, db_session, "Org Engine B", "engine-b@example.com")
    first = claim_batch(db_session, worker_id="w1")
    assert len(first) == 1
    second = claim_batch(db_session, worker_id="w2")
    assert second == []


def test_claim_batch_skips_future_next_retry_at(client: TestClient, db_session) -> None:
    run = _make_task_run(client, db_session, "Org Engine C", "engine-c@example.com")
    run.next_retry_at = datetime.now(timezone.utc) + timedelta(hours=1)
    db_session.commit()

    claimed = claim_batch(db_session, worker_id="w1")
    assert claimed == []


def test_claim_records_audit_event(client: TestClient, db_session) -> None:
    run = _make_task_run(client, db_session, "Org Engine D", "engine-d@example.com")
    claim_batch(db_session, worker_id="w1")
    events = db_session.query(TaskRunEvent).filter(TaskRunEvent.task_run_id == run.id).all()
    assert len(events) == 1
    assert events[0].event_type == "claimed"
    assert events[0].to_status == "running"


# --- Lease fencing (heartbeat / completion) -----------------------------------


def test_heartbeat_with_correct_lease_token_succeeds(client: TestClient, db_session) -> None:
    run = _make_task_run(client, db_session, "Org Engine E", "engine-e@example.com")
    claim_batch(db_session, worker_id="w1")
    db_session.refresh(run)
    old_expiry = run.lease_expires_at

    heartbeat(db_session, run.id, run.lease_token, worker_id="w1")
    db_session.refresh(run)
    assert run.lease_expires_at >= old_expiry
    assert run.last_heartbeat_at is not None


def test_heartbeat_with_wrong_lease_token_raises_lease_lost(client: TestClient, db_session) -> None:
    run = _make_task_run(client, db_session, "Org Engine F", "engine-f@example.com")
    claim_batch(db_session, worker_id="w1")
    with pytest.raises(LeaseLostError):
        heartbeat(db_session, run.id, uuid.uuid4(), worker_id="stale-worker")


def test_complete_success_with_wrong_lease_token_raises(client: TestClient, db_session) -> None:
    run = _make_task_run(client, db_session, "Org Engine G", "engine-g@example.com")
    claim_batch(db_session, worker_id="w1")
    with pytest.raises(LeaseLostError):
        complete_success(db_session, run.id, uuid.uuid4(), worker_id="stale-worker")

    db_session.refresh(run)
    assert run.status == TaskRunStatus.RUNNING  # untouched by the stale caller


def test_stale_worker_cannot_complete_after_reclaim(client: TestClient, db_session) -> None:
    """The exact scenario lease_token exists to prevent: a worker holds a
    now-stale lease_token (e.g. after the reaper reclaimed the row), and
    must not be able to complete work it no longer owns."""
    run = _make_task_run(client, db_session, "Org Engine H", "engine-h@example.com")
    claim_batch(db_session, worker_id="w1")
    db_session.refresh(run)
    stale_token = run.lease_token

    # Simulate the reaper reclaiming: force the row back to pending, then
    # let a second worker claim it fresh (gets a NEW lease_token).
    run.status = TaskRunStatus.PENDING
    run.started_at = None
    run.lease_token = None
    run.lease_expires_at = None
    db_session.commit()
    claim_batch(db_session, worker_id="w2")
    db_session.refresh(run)
    assert run.lease_token != stale_token

    with pytest.raises(LeaseLostError):
        complete_success(db_session, run.id, stale_token, worker_id="w1")


# --- Success / failure / retry ------------------------------------------------


def test_complete_success_clears_lease_and_sets_finished(client: TestClient, db_session) -> None:
    run = _make_task_run(client, db_session, "Org Engine I", "engine-i@example.com")
    claim_batch(db_session, worker_id="w1")
    db_session.refresh(run)

    complete_success(db_session, run.id, run.lease_token, worker_id="w1", log_output="ok")
    db_session.refresh(run)
    assert run.status == TaskRunStatus.SUCCESS
    assert run.finished_at is not None
    assert run.lease_token is None
    assert run.lease_expires_at is None
    assert run.log_output == "ok"


def test_complete_failure_retryable_requeues_to_pending(client: TestClient, db_session) -> None:
    run = _make_task_run(client, db_session, "Org Engine J", "engine-j@example.com")
    claim_batch(db_session, worker_id="w1")
    db_session.refresh(run)
    idem_key = run.idempotency_key

    complete_failure(db_session, run.id, run.lease_token, worker_id="w1", error_message="transient", retryable=True)
    db_session.refresh(run)
    assert run.status == TaskRunStatus.PENDING
    assert run.started_at is None
    assert run.finished_at is None
    assert run.error_message is None
    assert run.lease_token is None
    assert run.attempt_count == 1  # preserved, not reset
    assert run.next_retry_at is not None
    assert run.idempotency_key == idem_key  # stable across retries


def test_complete_failure_non_retryable_terminates_immediately(client: TestClient, db_session) -> None:
    run = _make_task_run(client, db_session, "Org Engine K", "engine-k@example.com")
    claim_batch(db_session, worker_id="w1")
    db_session.refresh(run)

    complete_failure(db_session, run.id, run.lease_token, worker_id="w1", error_message="bad config", retryable=False)
    db_session.refresh(run)
    assert run.status == TaskRunStatus.FAILED
    assert run.error_message == "bad config"
    assert run.finished_at is not None


def test_retry_exhausts_max_attempts_then_fails(client: TestClient, db_session) -> None:
    run = _make_task_run(client, db_session, "Org Engine L", "engine-l@example.com", max_attempts=2)
    task = db_session.get(Task, run.task_id)
    assert task.max_attempts == 2

    # attempt 1: claim + retryable failure -> requeued
    claim_batch(db_session, worker_id="w1")
    db_session.refresh(run)
    run.next_retry_at = None  # bypass backoff delay for the test
    db_session.commit()
    complete_failure(db_session, run.id, run.lease_token, worker_id="w1", error_message="e1", retryable=True)
    db_session.refresh(run)
    assert run.status == TaskRunStatus.PENDING
    assert run.attempt_count == 1

    # attempt 2: claim + retryable failure, but attempts are now exhausted -> failed
    run.next_retry_at = None
    db_session.commit()
    claim_batch(db_session, worker_id="w1")
    db_session.refresh(run)
    assert run.attempt_count == 2
    complete_failure(db_session, run.id, run.lease_token, worker_id="w1", error_message="e2", retryable=True)
    db_session.refresh(run)
    assert run.status == TaskRunStatus.FAILED
    assert run.error_message == "e2"


# --- DB-level constraint enforcement -----------------------------------------


def test_db_rejects_running_row_without_lease(client: TestClient, db_session) -> None:
    run = _make_task_run(client, db_session, "Org Engine M", "engine-m@example.com")
    run.status = TaskRunStatus.RUNNING
    run.started_at = datetime.now(timezone.utc)
    # lease_token / lease_expires_at deliberately left NULL
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_db_rejects_pending_row_with_lease_token_set(client: TestClient, db_session) -> None:
    run = _make_task_run(client, db_session, "Org Engine N", "engine-n@example.com")
    run.lease_token = uuid.uuid4()
    run.lease_expires_at = datetime.now(timezone.utc) + timedelta(minutes=5)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_db_rejects_duplicate_idempotency_key(client: TestClient, db_session) -> None:
    run = _make_task_run(client, db_session, "Org Engine O", "engine-o@example.com")
    dup = TaskRun(
        organization_id=run.organization_id,
        task_id=run.task_id,
        idempotency_key=run.idempotency_key,
    )
    db_session.add(dup)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
