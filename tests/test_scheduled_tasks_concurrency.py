"""Module 12: PostgreSQL-only concurrency and constraint-existence proof.

Genuine duplicate-prevention under concurrent scheduler workers can only be
proven against real PostgreSQL row locking (SELECT ... FOR UPDATE SKIP
LOCKED) -- SQLite has no real row-level locking, so every test in this
file is skipped there, exactly as this project's own testing convention
requires (see docs/module-12-scheduled-task-execution-design.md and the
identical precedent already established for claim_batch's own concurrency
guarantee in prior modules)."""
import threading
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db.session import SessionLocal
from app.models.task import Task
from app.models.task_run import TaskRun
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


def _require_postgresql(db_session) -> None:
    if db_session.get_bind().dialect.name != "postgresql":
        pytest.skip("Real concurrency/constraint verification requires PostgreSQL")


def test_two_concurrent_scheduler_workers_never_duplicate_a_due_occurrence(
    client: TestClient, db_session
) -> None:
    _require_postgresql(db_session)

    headers = _auth_headers(client, "Org Concurrency A", "concurrency-a@example.com")
    resp = client.post(
        "/tasks",
        json={"name": "Concurrent Sync", "task_type": "sync", "schedule_interval_seconds": 300},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    task_id = uuid.UUID(resp.json()["id"])

    task = db_session.get(Task, task_id)
    task.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=60)
    db_session.commit()

    barrier = threading.Barrier(2)
    results: list[int] = []
    errors: list[Exception] = []

    def _worker(worker_id: str) -> None:
        session = SessionLocal()
        try:
            barrier.wait(timeout=5)
            created = run_due_schedules(session, worker_id=worker_id, batch_size=10)
            results.append(created)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            session.close()

    t1 = threading.Thread(target=_worker, args=("scheduler-1",))
    t2 = threading.Thread(target=_worker, args=("scheduler-2",))
    t1.start()
    t2.start()
    t1.join(timeout=15)
    t2.join(timeout=15)

    assert not errors, f"worker threads raised: {errors}"
    # Exactly one of the two concurrent passes created the run; the other
    # found nothing due (SKIP LOCKED skipped the already-locked row, or the
    # guarded UPDATE's rowcount guard caught the race).
    assert sorted(results) == [0, 1]

    runs = db_session.query(TaskRun).filter(TaskRun.task_id == task_id).all()
    assert len(runs) == 1


def test_concurrent_workers_across_multiple_due_tasks_create_exactly_one_run_each(
    client: TestClient, db_session
) -> None:
    _require_postgresql(db_session)

    headers = _auth_headers(client, "Org Concurrency B", "concurrency-b@example.com")
    task_ids = []
    for i in range(6):
        resp = client.post(
            "/tasks",
            json={
                "name": f"Concurrent Sync {i}",
                "task_type": "sync",
                "schedule_interval_seconds": 300,
            },
            headers=headers,
        )
        assert resp.status_code == 201, resp.text
        tid = uuid.UUID(resp.json()["id"])
        task = db_session.get(Task, tid)
        task.next_run_at = datetime.now(timezone.utc) - timedelta(seconds=60)
        task_ids.append(tid)
    db_session.commit()

    barrier = threading.Barrier(2)
    results: list[int] = []
    errors: list[Exception] = []

    def _worker(worker_id: str) -> None:
        session = SessionLocal()
        try:
            barrier.wait(timeout=5)
            created = run_due_schedules(session, worker_id=worker_id, batch_size=10)
            results.append(created)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)
        finally:
            session.close()

    threads = [threading.Thread(target=_worker, args=(f"scheduler-{i}",)) for i in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=15)

    assert not errors, f"worker threads raised: {errors}"
    assert sum(results) == 6

    for tid in task_ids:
        runs = db_session.query(TaskRun).filter(TaskRun.task_id == tid).all()
        assert len(runs) == 1, f"task {tid} got {len(runs)} runs, expected exactly 1"


def test_partial_due_task_index_exists(client: TestClient, db_session) -> None:
    _require_postgresql(db_session)
    row = db_session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_tasks_scheduled_due'")
    ).first()
    assert row is not None
    assert "next_run_at" in row[0]
    assert "schedule_interval_seconds IS NOT NULL" in row[0]
    assert "is_active" in row[0]


def test_schedule_check_constraints_exist(client: TestClient, db_session) -> None:
    _require_postgresql(db_session)
    rows = db_session.execute(
        text(
            "SELECT conname FROM pg_constraint WHERE conrelid = 'tasks'::regclass "
            "AND conname IN ('ck_tasks_schedule_interval_hard_floor', "
            "'ck_tasks_schedule_consistency')"
        )
    ).all()
    names = {r[0] for r in rows}
    assert names == {"ck_tasks_schedule_interval_hard_floor", "ck_tasks_schedule_consistency"}


def test_org_name_active_index_survived_the_migration(client: TestClient, db_session) -> None:
    """Regression guard for the index-loss bug discovered and fixed while
    authoring database/alembic/versions/d5e6f7a8b9c0 (SQLite's
    batch_alter_table table-copy silently dropped this unreflectable
    expression index) -- confirms it on PostgreSQL too, where
    batch_alter_table never goes through a table-copy at all, so this
    should trivially hold, but is asserted explicitly rather than assumed."""
    _require_postgresql(db_session)
    row = db_session.execute(
        text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_tasks_org_name_active'")
    ).first()
    assert row is not None


def test_hard_floor_constraint_rejects_interval_below_30_seconds(client: TestClient, db_session) -> None:
    _require_postgresql(db_session)
    headers = _auth_headers(client, "Org Concurrency C", "concurrency-c@example.com")
    resp = client.post(
        "/tasks",
        json={"name": "Sync", "task_type": "sync"},
        headers=headers,
    )
    task_id = uuid.UUID(resp.json()["id"])

    from sqlalchemy.exc import IntegrityError

    # Bypass the Pydantic/application-layer minimum entirely -- proves the
    # database-level hard floor is a real, independent backstop, not just
    # documentation.
    with pytest.raises(IntegrityError):
        db_session.execute(
            text(
                "UPDATE tasks SET schedule_interval_seconds = 10, "
                "next_run_at = now() WHERE id = :id"
            ),
            {"id": str(task_id)},
        )
        db_session.commit()
    db_session.rollback()
