"""Tests confirming the required execution-engine metrics (claimed,
completed, failed, retried, execution duration, queue depth) actually move
when the engine performs the corresponding action."""
import uuid

from fastapi.testclient import TestClient

from app.models.task_run import TaskRun
from app.worker import metrics
from app.worker.engine import claim_batch, complete_failure, complete_success


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


def _make_task_run(client: TestClient, db_session, org_name: str, email: str) -> TaskRun:
    headers = _auth_headers(client, org_name, email)
    task_resp = client.post("/tasks", json={"name": "Nightly Sync", "task_type": "sync"}, headers=headers)
    run_resp = client.post(f"/tasks/{task_resp.json()['id']}/runs", headers=headers)
    return db_session.get(TaskRun, uuid.UUID(run_resp.json()["id"]))


def _counter_value(counter) -> float:
    return counter.collect()[0].samples[0].value


def test_claim_increments_claimed_counter(client: TestClient, db_session) -> None:
    before = _counter_value(metrics.tasks_claimed_total)
    _make_task_run(client, db_session, "Org Metrics A", "metrics-a@example.com")
    claim_batch(db_session, worker_id="w1")
    assert _counter_value(metrics.tasks_claimed_total) == before + 1


def test_success_increments_completed_and_duration(client: TestClient, db_session) -> None:
    before = _counter_value(metrics.tasks_completed_total)
    run = _make_task_run(client, db_session, "Org Metrics B", "metrics-b@example.com")
    claim_batch(db_session, worker_id="w1")
    db_session.refresh(run)
    complete_success(db_session, run.id, run.lease_token, worker_id="w1")
    assert _counter_value(metrics.tasks_completed_total) == before + 1
    duration_samples = [s for s in metrics.task_execution_duration_seconds.collect()[0].samples if s.name.endswith("_count")]
    assert duration_samples[0].value >= 1


def test_failure_increments_failed_counter(client: TestClient, db_session) -> None:
    before = _counter_value(metrics.tasks_failed_total)
    run = _make_task_run(client, db_session, "Org Metrics C", "metrics-c@example.com")
    claim_batch(db_session, worker_id="w1")
    db_session.refresh(run)
    complete_failure(db_session, run.id, run.lease_token, worker_id="w1", error_message="bad", retryable=False)
    assert _counter_value(metrics.tasks_failed_total) == before + 1


def test_retryable_failure_increments_retried_counter(client: TestClient, db_session) -> None:
    before = _counter_value(metrics.tasks_retried_total)
    run = _make_task_run(client, db_session, "Org Metrics D", "metrics-d@example.com")
    claim_batch(db_session, worker_id="w1")
    db_session.refresh(run)
    complete_failure(db_session, run.id, run.lease_token, worker_id="w1", error_message="transient", retryable=True)
    assert _counter_value(metrics.tasks_retried_total) == before + 1


def test_queue_depth_reflects_pending_count(client: TestClient, db_session) -> None:
    _make_task_run(client, db_session, "Org Metrics E", "metrics-e@example.com")
    # batch_size=0 claims nothing but still recomputes the gauge from a live
    # DB count (claim_batch always refreshes queue_depth at the end).
    claim_batch(db_session, worker_id="probe", batch_size=0)
    assert metrics.queue_depth.collect()[0].samples[0].value == 1.0

    claimed = claim_batch(db_session, worker_id="w1", batch_size=1)
    assert len(claimed) == 1
    assert metrics.queue_depth.collect()[0].samples[0].value == 0.0
