"""Tests for the stuck-run reaper: expired leases are recovered (requeued
or failed, exactly as a worker-reported failure would be), and unexpired
running rows are left alone."""
import uuid
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.models.enums import TaskRunStatus
from app.models.task_run import TaskRun
from app.models.task_run_event import TaskRunEvent
from app.worker.engine import claim_batch
from app.worker.reaper import reap_expired_runs


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


def test_reaper_requeues_expired_lease_with_attempts_remaining(client: TestClient, db_session) -> None:
    run = _make_task_run(client, db_session, "Org Reaper A", "reaper-a@example.com")
    claim_batch(db_session, worker_id="crashed-worker")
    db_session.refresh(run)
    run.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()

    recovered = reap_expired_runs(db_session)
    assert recovered == 1

    db_session.refresh(run)
    assert run.status == TaskRunStatus.PENDING
    assert run.lease_token is None
    assert run.next_retry_at is not None
    assert run.attempt_count == 1  # preserved


def test_reaper_terminates_when_attempts_exhausted(client: TestClient, db_session) -> None:
    run = _make_task_run(client, db_session, "Org Reaper B", "reaper-b@example.com", max_attempts=1)
    claim_batch(db_session, worker_id="crashed-worker")
    db_session.refresh(run)
    run.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()

    reap_expired_runs(db_session)
    db_session.refresh(run)
    assert run.status == TaskRunStatus.FAILED
    assert "lease expired" in run.error_message.lower() or "timed out" in run.error_message.lower()


def test_reaper_leaves_unexpired_running_rows_alone(client: TestClient, db_session) -> None:
    run = _make_task_run(client, db_session, "Org Reaper C", "reaper-c@example.com")
    claim_batch(db_session, worker_id="w1")
    db_session.refresh(run)
    assert run.lease_expires_at is not None  # freshly claimed -> well in the future, not expired

    recovered = reap_expired_runs(db_session)
    assert recovered == 0
    db_session.refresh(run)
    assert run.status == TaskRunStatus.RUNNING


def test_reaper_leaves_pending_rows_alone(client: TestClient, db_session) -> None:
    run = _make_task_run(client, db_session, "Org Reaper D", "reaper-d@example.com")
    recovered = reap_expired_runs(db_session)
    assert recovered == 0
    db_session.refresh(run)
    assert run.status == TaskRunStatus.PENDING


def test_reaper_writes_reaped_audit_event(client: TestClient, db_session) -> None:
    run = _make_task_run(client, db_session, "Org Reaper E", "reaper-e@example.com")
    claim_batch(db_session, worker_id="crashed-worker")
    db_session.refresh(run)
    run.lease_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    db_session.commit()

    reap_expired_runs(db_session)
    events = db_session.query(TaskRunEvent).filter(TaskRunEvent.task_run_id == run.id).all()
    event_types = [e.event_type for e in events]
    assert "claimed" in event_types
    assert "requeued" in event_types
