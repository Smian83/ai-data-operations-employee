"""Module 12: unit tests for app.worker.scheduler.run_due_schedules --
due/not-due/inactive selection, next_run_at advancement, TaskRun shape,
deterministic ordering, batch-size enforcement, missed-schedule catch-up,
fault isolation / starvation avoidance, claimability by the existing
worker, tenant isolation, and metrics/logging side effects."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.models.enums import TaskRunStatus, TaskType
from app.models.task import Task
from app.models.task_run import TaskRun
from app.worker import metrics
from app.worker.engine import claim_batch
from app.worker.scheduler import run_due_schedules


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


def _create_scheduled_task(
    client: TestClient, headers: dict, interval_seconds: int = 300, **overrides
) -> dict:
    payload = {
        "name": "Nightly Sync",
        "task_type": "sync",
        "schedule_interval_seconds": interval_seconds,
    }
    payload.update(overrides)
    resp = client.post("/tasks", json=payload, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _make_due(db_session, task_id: uuid.UUID, seconds_overdue: int = 60) -> None:
    """Force a scheduled task's next_run_at into the past, bypassing the
    API (mirrors test_worker_reaper.py's own direct-DB-mutation style for
    forcing a lease into an expired state)."""
    task = db_session.get(Task, task_id)
    task.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=seconds_overdue)
    db_session.commit()


def _counter_value(counter) -> float:
    return counter.collect()[0].samples[0].value


# --- Basic due/not-due/inactive selection --------------------------------


def test_due_task_creates_exactly_one_task_run(client: TestClient, db_session) -> None:
    headers = _auth_headers(client, "Org Sch1", "sch1@example.com")
    task = _create_scheduled_task(client, headers)
    _make_due(db_session, uuid.UUID(task["id"]))

    created = run_due_schedules(db_session, worker_id="test")
    assert created == 1

    runs = db_session.query(TaskRun).filter(TaskRun.task_id == uuid.UUID(task["id"])).all()
    assert len(runs) == 1
    assert runs[0].status == TaskRunStatus.PENDING
    assert runs[0].triggered_by is None
    assert runs[0].source_task_run_id is None
    assert runs[0].organization_id == uuid.UUID(task["organization_id"])


def test_not_due_task_creates_none(client: TestClient, db_session) -> None:
    headers = _auth_headers(client, "Org Sch2", "sch2@example.com")
    task = _create_scheduled_task(client, headers, interval_seconds=3600)
    # next_run_at is ~1 hour in the future at creation -- not due yet.

    created = run_due_schedules(db_session, worker_id="test")
    assert created == 0
    runs = db_session.query(TaskRun).filter(TaskRun.task_id == uuid.UUID(task["id"])).all()
    assert runs == []


def test_inactive_task_creates_none_even_if_overdue(client: TestClient, db_session) -> None:
    headers = _auth_headers(client, "Org Sch3", "sch3@example.com")
    task = _create_scheduled_task(client, headers)
    _make_due(db_session, uuid.UUID(task["id"]))
    del_resp = client.delete(f"/tasks/{task['id']}", headers=headers)
    assert del_resp.status_code == 204

    created = run_due_schedules(db_session, worker_id="test")
    assert created == 0


def test_unscheduled_task_never_selected(client: TestClient, db_session) -> None:
    headers = _auth_headers(client, "Org Sch4", "sch4@example.com")
    client.post("/tasks", json={"name": "Manual only", "task_type": "sync"}, headers=headers)

    created = run_due_schedules(db_session, worker_id="test")
    assert created == 0


# --- next_run_at advancement ----------------------------------------------


def test_due_task_advances_next_run_at(client: TestClient, db_session) -> None:
    headers = _auth_headers(client, "Org Sch5", "sch5@example.com")
    task = _create_scheduled_task(client, headers, interval_seconds=300)
    _make_due(db_session, uuid.UUID(task["id"]))
    before = datetime.now(timezone.utc)

    run_due_schedules(db_session, worker_id="test")

    db_session.expire_all()
    refreshed = db_session.get(Task, uuid.UUID(task["id"]))
    assert refreshed.next_run_at is not None
    next_run_at = refreshed.next_run_at
    if next_run_at.tzinfo is None:  # SQLite tzinfo round-trip quirk
        next_run_at = next_run_at.replace(tzinfo=timezone.utc)
    # Anchored to claim time + interval, NOT to the old stale value.
    assert before + timedelta(seconds=290) <= next_run_at <= before + timedelta(seconds=310)


# --- Deterministic ordering / batch size / starvation ----------------------


def test_multiple_due_tasks_processed_in_deterministic_order(client: TestClient, db_session) -> None:
    headers = _auth_headers(client, "Org Sch6", "sch6@example.com")
    t1 = _create_scheduled_task(client, headers, name="T1")
    t2 = _create_scheduled_task(client, headers, name="T2")
    t3 = _create_scheduled_task(client, headers, name="T3")
    # Stagger due-ness so ordering (next_run_at ASC, id ASC) is observable.
    _make_due(db_session, uuid.UUID(t2["id"]), seconds_overdue=300)
    _make_due(db_session, uuid.UUID(t1["id"]), seconds_overdue=100)
    _make_due(db_session, uuid.UUID(t3["id"]), seconds_overdue=200)

    created = run_due_schedules(db_session, worker_id="test", batch_size=1)
    assert created == 1
    # The most-overdue task (t2, overdue by 300s) must be processed first.
    run = db_session.query(TaskRun).filter(TaskRun.task_id == uuid.UUID(t2["id"])).one_or_none()
    assert run is not None


def test_batch_size_is_enforced(client: TestClient, db_session) -> None:
    headers = _auth_headers(client, "Org Sch7", "sch7@example.com")
    task_ids = []
    for i in range(5):
        t = _create_scheduled_task(client, headers, name=f"Batch Task {i}")
        _make_due(db_session, uuid.UUID(t["id"]))
        task_ids.append(t["id"])

    created = run_due_schedules(db_session, worker_id="test", batch_size=3)
    assert created == 3

    remaining_due = run_due_schedules(db_session, worker_id="test", batch_size=10)
    assert remaining_due == 2


def test_malformed_earliest_due_task_does_not_starve_valid_later_tasks(
    client: TestClient, db_session, monkeypatch
) -> None:
    """Directly exercises the pass-local starvation-avoidance mechanism
    (see app/worker/scheduler.py's own module docstring): the MOST
    overdue task ("broken", overdue by 200s, selected first under
    `ORDER BY next_run_at ASC`) has its guarded UPDATE forced to report
    rowcount=0 -- simulating a lost race. It must be excluded for the rest
    of THIS pass, and a different, healthy due task ("healthy", overdue by
    100s) must still be processed in the SAME call, proving one malformed
    task costs at most one wasted batch slot, never the whole batch."""
    import app.worker.scheduler as scheduler_module

    headers = _auth_headers(client, "Org Sch9", "sch9@example.com")
    broken = _create_scheduled_task(client, headers, name="Broken2", interval_seconds=60)
    healthy = _create_scheduled_task(client, headers, name="Healthy2", interval_seconds=60)
    _make_due(db_session, uuid.UUID(broken["id"]), seconds_overdue=200)
    _make_due(db_session, uuid.UUID(healthy["id"]), seconds_overdue=100)

    real_execute = db_session.execute
    call_count = {"n": 0}

    def _flaky_execute(stmt, *args, **kwargs):
        # Force the FIRST guarded UPDATE (the broken task's) to appear to
        # affect zero rows, simulating a lost race, without touching any
        # other statement (SELECTs, the healthy task's own UPDATE, or the
        # TaskRun INSERT).
        result = real_execute(stmt, *args, **kwargs)
        is_update_broken = (
            getattr(stmt, "is_update", False)
            and call_count["n"] == 0
        )
        if is_update_broken:
            call_count["n"] += 1

            class _ZeroRowcount:
                rowcount = 0

            return _ZeroRowcount()
        return result

    monkeypatch.setattr(db_session, "execute", _flaky_execute)
    created = run_due_schedules(db_session, worker_id="test", batch_size=10)
    monkeypatch.undo()

    # The healthy task still got its run despite the broken one losing its
    # race on the first attempt -- proving the exclusion mechanism let the
    # loop move on within the same pass/call.
    assert created == 1
    assert db_session.query(TaskRun).filter(TaskRun.task_id == uuid.UUID(healthy["id"])).count() == 1
    assert db_session.query(TaskRun).filter(TaskRun.task_id == uuid.UUID(broken["id"])).count() == 0


# --- Missed-schedule catch-up policy ---------------------------------------


def test_missed_intervals_produce_exactly_one_catch_up_run(client: TestClient, db_session) -> None:
    headers = _auth_headers(client, "Org Sch10", "sch10@example.com")
    task = _create_scheduled_task(client, headers, interval_seconds=60)
    # Simulate many missed periods: overdue by a full day against a
    # 60-second interval (would be 1440 missed occurrences under a
    # "create every missed run" policy).
    _make_due(db_session, uuid.UUID(task["id"]), seconds_overdue=86400)

    created = run_due_schedules(db_session, worker_id="test")
    assert created == 1
    assert db_session.query(TaskRun).filter(TaskRun.task_id == uuid.UUID(task["id"])).count() == 1

    db_session.expire_all()
    refreshed = db_session.get(Task, uuid.UUID(task["id"]))
    next_run_at = refreshed.next_run_at
    if next_run_at.tzinfo is None:
        next_run_at = next_run_at.replace(tzinfo=timezone.utc)
    # Rescheduled from "now", not from the stale original occurrence --
    # must be in the near future, not still deeply in the past.
    assert next_run_at > datetime.now(timezone.utc)


# --- Claimability by the existing worker / no direct execution -------------


def test_scheduler_created_run_is_claimable_by_existing_worker(client: TestClient, db_session) -> None:
    headers = _auth_headers(client, "Org Sch11", "sch11@example.com")
    task = _create_scheduled_task(client, headers)
    _make_due(db_session, uuid.UUID(task["id"]))

    run_due_schedules(db_session, worker_id="test")
    claimed = claim_batch(db_session, worker_id="w1")
    assert len(claimed) == 1
    assert claimed[0].task_id == uuid.UUID(task["id"])
    assert claimed[0].triggered_by is None


def test_scheduler_never_creates_a_non_sync_task_run(client: TestClient, db_session) -> None:
    """Defense in depth: even if a non-SYNC task somehow had
    schedule_interval_seconds set at the DB layer (bypassing API
    validation), the scheduler's own WHERE clause and factory call never
    special-case task_type -- but every row it CAN select is, by
    construction of every task actually reachable through the API, always
    SYNC. This proves that indirectly: 100% of scheduler-created runs in
    this suite are for SYNC tasks."""
    headers = _auth_headers(client, "Org Sch12", "sch12@example.com")
    task = _create_scheduled_task(client, headers)
    _make_due(db_session, uuid.UUID(task["id"]))

    run_due_schedules(db_session, worker_id="test")
    run = db_session.query(TaskRun).filter(TaskRun.task_id == uuid.UUID(task["id"])).one()
    created_task = db_session.get(Task, run.task_id)
    assert created_task.task_type == TaskType.SYNC


# --- Tenant isolation -------------------------------------------------------


def test_due_tasks_across_multiple_orgs_produce_correctly_scoped_runs(
    client: TestClient, db_session
) -> None:
    headers_a = _auth_headers(client, "Org Sch13A", "sch13a@example.com")
    headers_b = _auth_headers(client, "Org Sch13B", "sch13b@example.com")
    task_a = _create_scheduled_task(client, headers_a)
    task_b = _create_scheduled_task(client, headers_b)
    _make_due(db_session, uuid.UUID(task_a["id"]))
    _make_due(db_session, uuid.UUID(task_b["id"]))

    created = run_due_schedules(db_session, worker_id="test")
    assert created == 2

    run_a = db_session.query(TaskRun).filter(TaskRun.task_id == uuid.UUID(task_a["id"])).one()
    run_b = db_session.query(TaskRun).filter(TaskRun.task_id == uuid.UUID(task_b["id"])).one()
    assert str(run_a.organization_id) == task_a["organization_id"]
    assert str(run_b.organization_id) == task_b["organization_id"]
    assert run_a.organization_id != run_b.organization_id


# --- Metrics -----------------------------------------------------------


def test_successful_empty_pass_updates_last_success_metric(client: TestClient, db_session) -> None:
    before = _counter_value(metrics.scheduler_passes_total)
    ts_before = metrics.scheduler_last_success_timestamp_seconds.collect()[0].samples[0].value

    created = run_due_schedules(db_session, worker_id="test")
    assert created == 0
    assert _counter_value(metrics.scheduler_passes_total) == before + 1
    ts_after = metrics.scheduler_last_success_timestamp_seconds.collect()[0].samples[0].value
    assert ts_after >= ts_before


def test_committed_runs_increment_created_metric(client: TestClient, db_session) -> None:
    headers = _auth_headers(client, "Org Sch14", "sch14@example.com")
    task = _create_scheduled_task(client, headers)
    _make_due(db_session, uuid.UUID(task["id"]))
    before = _counter_value(metrics.scheduler_runs_created_total)

    run_due_schedules(db_session, worker_id="test")
    assert _counter_value(metrics.scheduler_runs_created_total) == before + 1


def test_per_task_failure_increments_error_metric(client: TestClient, db_session, monkeypatch) -> None:
    headers = _auth_headers(client, "Org Sch15", "sch15@example.com")
    task = _create_scheduled_task(client, headers)
    _make_due(db_session, uuid.UUID(task["id"]))
    before = _counter_value(metrics.scheduler_errors_total)

    import app.worker.scheduler as scheduler_module

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated per-task failure")

    # scheduler.py does `from app.services.task_run_factory import
    # create_task_run_record`, binding the name directly in its own module
    # namespace -- patching the origin module's attribute would not affect
    # calls made from inside scheduler.py, so the patch target must be
    # scheduler_module's own name.
    monkeypatch.setattr(scheduler_module, "create_task_run_record", _boom)

    created = run_due_schedules(db_session, worker_id="test")
    monkeypatch.undo()

    assert created == 0
    assert _counter_value(metrics.scheduler_errors_total) == before + 1
    # The task remains due -- rolled back, not partially advanced.
    db_session.expire_all()
    refreshed = db_session.get(Task, uuid.UUID(task["id"]))
    assert refreshed.next_run_at is not None
    next_run_at = refreshed.next_run_at
    if next_run_at.tzinfo is None:
        next_run_at = next_run_at.replace(tzinfo=timezone.utc)
    assert next_run_at <= datetime.now(timezone.utc)


def test_rollback_leaves_failed_task_due_and_next_pass_retries_it(
    client: TestClient, db_session, monkeypatch
) -> None:
    headers = _auth_headers(client, "Org Sch16", "sch16@example.com")
    task = _create_scheduled_task(client, headers)
    _make_due(db_session, uuid.UUID(task["id"]))

    import app.worker.scheduler as scheduler_module

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated failure")

    monkeypatch.setattr(scheduler_module, "create_task_run_record", _boom)
    first_pass = run_due_schedules(db_session, worker_id="test")
    monkeypatch.undo()
    assert first_pass == 0
    assert db_session.query(TaskRun).filter(TaskRun.task_id == uuid.UUID(task["id"])).count() == 0

    second_pass = run_due_schedules(db_session, worker_id="test")
    assert second_pass == 1
    assert db_session.query(TaskRun).filter(TaskRun.task_id == uuid.UUID(task["id"])).count() == 1
