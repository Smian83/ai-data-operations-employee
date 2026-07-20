"""Tests for /tasks and /tasks/{id}/runs: CRUD, tenant-aware FK integrity
(including direct DB-level checks bypassing the API), inactive-resource
restrictions, TaskRun state invariants, and pagination."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError


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


def _create_source(client: TestClient, headers: dict, **overrides) -> dict:
    payload = {
        "name": "Primary Postgres",
        "source_type": "postgres",
        "connection_metadata": {},
    }
    payload.update(overrides)
    return client.post("/data-sources", json=payload, headers=headers)


def _create_task(client: TestClient, headers: dict, **overrides) -> dict:
    payload = {"name": "Nightly Sync", "task_type": "sync"}
    payload.update(overrides)
    return client.post("/tasks", json=payload, headers=headers)


# --- Create ----------------------------------------------------------------


def test_create_task_success_with_data_source(client: TestClient) -> None:
    headers = _auth_headers(client, "Org A", "a@example.com")
    ds = _create_source(client, headers).json()
    resp = _create_task(client, headers, data_source_id=ds["id"])
    assert resp.status_code == 201, resp.text
    assert resp.json()["data_source_id"] == ds["id"]


def test_create_task_without_data_source(client: TestClient) -> None:
    headers = _auth_headers(client, "Org B", "b@example.com")
    resp = _create_task(client, headers)
    assert resp.status_code == 201
    assert resp.json()["data_source_id"] is None


def test_create_task_with_cross_org_data_source_404(client: TestClient) -> None:
    headers_a = _auth_headers(client, "Org C1", "c1@example.com")
    headers_b = _auth_headers(client, "Org C2", "c2@example.com")
    ds_a = _create_source(client, headers_a).json()

    resp = _create_task(client, headers_b, data_source_id=ds_a["id"])
    assert resp.status_code == 404


def test_create_task_with_nonexistent_data_source_404(client: TestClient) -> None:
    headers = _auth_headers(client, "Org D", "d@example.com")
    resp = _create_task(client, headers, data_source_id=str(uuid.uuid4()))
    assert resp.status_code == 404


def test_create_task_with_inactive_data_source_404(client: TestClient) -> None:
    headers = _auth_headers(client, "Org E", "e@example.com")
    ds = _create_source(client, headers).json()
    client.delete(f"/data-sources/{ds['id']}", headers=headers)

    resp = _create_task(client, headers, data_source_id=ds["id"])
    assert resp.status_code == 404


def test_create_task_rejects_invalid_task_type(client: TestClient) -> None:
    headers = _auth_headers(client, "Org F", "f@example.com")
    resp = _create_task(client, headers, task_type="not_real")
    assert resp.status_code == 422


def test_create_task_rejects_client_supplied_organization_id(client: TestClient) -> None:
    headers = _auth_headers(client, "Org G", "g@example.com")
    resp = client.post(
        "/tasks",
        json={"name": "X", "task_type": "sync", "organization_id": str(uuid.uuid4())},
        headers=headers,
    )
    assert resp.status_code == 422


def test_task_created_by_is_always_current_user(client: TestClient, db_session) -> None:
    from app.models.task import Task
    from app.models.user import User

    headers = _auth_headers(client, "Org H", "h@example.com")
    resp = _create_task(client, headers)
    task = db_session.get(Task, uuid.UUID(resp.json()["id"]))
    user = db_session.query(User).filter(User.email == "h@example.com").one()
    assert task.created_by == user.id


def test_create_task_unauthenticated_rejected(client: TestClient) -> None:
    resp = client.post("/tasks", json={"name": "X", "task_type": "sync"})
    assert resp.status_code == 401


# --- Database-level tenant-aware FK integrity (bypassing the API) ------------


def test_db_level_task_org_data_source_mismatch_rejected(
    client: TestClient, db_session
) -> None:
    """Directly inserting a Task whose organization_id does not match its
    data_source's real organization_id must fail at the DATABASE level,
    proving the composite FK — not just application code — enforces this."""
    from app.models.data_source import DataSource
    from app.models.task import Task
    from app.models.enums import SourceType, TaskType

    import sqlalchemy as sa

    headers_a = _auth_headers(client, "Org I1", "i1@example.com")
    headers_b = _auth_headers(client, "Org I2", "i2@example.com")
    ds_a = _create_source(client, headers_a).json()

    raw_org_b_id = db_session.execute(
        sa.text("SELECT organization_id FROM users WHERE email = 'i2@example.com'")
    ).scalar_one()
    org_b_id = raw_org_b_id if isinstance(raw_org_b_id, uuid.UUID) else uuid.UUID(str(raw_org_b_id))

    bad_task = Task(
        organization_id=org_b_id,  # org B ...
        data_source_id=uuid.UUID(ds_a["id"]),  # ... pointing at org A's data source
        name="Mismatched Task",
        task_type=TaskType.SYNC,
    )
    db_session.add(bad_task)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


# --- Name uniqueness (case-insensitive, per-org) --------------------------


def test_task_case_insensitive_name_collision_rejected(client: TestClient) -> None:
    headers = _auth_headers(client, "Org J", "j@example.com")
    assert _create_task(client, headers, name="Nightly Sync").status_code == 201
    resp = _create_task(client, headers, name="  NIGHTLY sync  ")
    assert resp.status_code == 409


def test_task_name_reusable_after_soft_delete(client: TestClient) -> None:
    headers = _auth_headers(client, "Org K", "k@example.com")
    r1 = _create_task(client, headers, name="Reusable Task")
    task_id = r1.json()["id"]
    assert client.delete(f"/tasks/{task_id}", headers=headers).status_code == 204

    r2 = _create_task(client, headers, name="Reusable Task")
    assert r2.status_code == 201


# --- Soft-delete / inactive restrictions ------------------------------------


def test_task_get_patch_delete_on_inactive_returns_404(client: TestClient) -> None:
    headers = _auth_headers(client, "Org L", "l@example.com")
    r1 = _create_task(client, headers, name="Soon Inactive")
    task_id = r1.json()["id"]
    assert client.delete(f"/tasks/{task_id}", headers=headers).status_code == 204

    assert client.get(f"/tasks/{task_id}", headers=headers).status_code == 404
    assert client.patch(
        f"/tasks/{task_id}", json={"name": "New"}, headers=headers
    ).status_code == 404
    assert client.delete(f"/tasks/{task_id}", headers=headers).status_code == 404


def test_task_list_excludes_inactive_by_default(client: TestClient) -> None:
    headers = _auth_headers(client, "Org M", "m@example.com")
    r1 = _create_task(client, headers, name="Will Delete")
    client.delete(f"/tasks/{r1.json()['id']}", headers=headers)

    assert client.get("/tasks", headers=headers).json()["total"] == 0
    assert client.get("/tasks?include_inactive=true", headers=headers).json()["total"] == 1


# --- Cross-tenant isolation --------------------------------------------------


def test_task_cross_tenant_access_404(client: TestClient) -> None:
    headers_a = _auth_headers(client, "Org N1", "n1@example.com")
    headers_b = _auth_headers(client, "Org N2", "n2@example.com")
    task_id = _create_task(client, headers_a, name="Org A Task").json()["id"]

    assert client.get(f"/tasks/{task_id}", headers=headers_b).status_code == 404
    assert client.patch(
        f"/tasks/{task_id}", json={"name": "Hijacked"}, headers=headers_b
    ).status_code == 404
    assert client.delete(f"/tasks/{task_id}", headers=headers_b).status_code == 404


# --- Pagination --------------------------------------------------------------


def test_task_list_pagination(client: TestClient) -> None:
    headers = _auth_headers(client, "Org O", "o@example.com")
    for i in range(3):
        _create_task(client, headers, name=f"Task {i}")

    resp = client.get("/tasks", headers=headers)
    body = resp.json()
    assert body["limit"] == 50 and body["offset"] == 0 and body["total"] == 3

    assert client.get("/tasks?limit=101", headers=headers).status_code == 422
    assert client.get("/tasks?limit=100", headers=headers).status_code == 200


# =============================================================================
# TaskRun tests
# =============================================================================


def test_create_run_on_active_task_success(client: TestClient, db_session) -> None:
    from app.models.user import User

    headers = _auth_headers(client, "Org P", "p@example.com")
    task_id = _create_task(client, headers).json()["id"]

    resp = client.post(f"/tasks/{task_id}/runs", headers=headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["status"] == "pending"
    assert body["task_id"] == task_id
    assert body["started_at"] is None
    assert body["finished_at"] is None

    user = db_session.query(User).filter(User.email == "p@example.com").one()
    assert body["triggered_by"] == str(user.id)


def test_create_run_on_inactive_task_404(client: TestClient) -> None:
    headers = _auth_headers(client, "Org Q", "q@example.com")
    task_id = _create_task(client, headers).json()["id"]
    client.delete(f"/tasks/{task_id}", headers=headers)

    resp = client.post(f"/tasks/{task_id}/runs", headers=headers)
    assert resp.status_code == 404


def test_create_run_on_cross_tenant_task_404(client: TestClient) -> None:
    headers_a = _auth_headers(client, "Org R1", "r1@example.com")
    headers_b = _auth_headers(client, "Org R2", "r2@example.com")
    task_id = _create_task(client, headers_a).json()["id"]

    resp = client.post(f"/tasks/{task_id}/runs", headers=headers_b)
    assert resp.status_code == 404


def test_create_run_unauthenticated_rejected(client: TestClient) -> None:
    headers = _auth_headers(client, "Org S", "s@example.com")
    task_id = _create_task(client, headers).json()["id"]
    resp = client.post(f"/tasks/{task_id}/runs")
    assert resp.status_code == 401


def test_db_level_task_run_org_task_mismatch_rejected(client: TestClient, db_session) -> None:
    """Directly inserting a TaskRun whose organization_id doesn't match its
    task's real organization_id must fail at the DATABASE level."""
    import sqlalchemy as sa
    from app.models.task_run import TaskRun

    headers_a = _auth_headers(client, "Org T1", "t1@example.com")
    headers_b = _auth_headers(client, "Org T2", "t2@example.com")
    task_a_id = _create_task(client, headers_a).json()["id"]
    raw_org_b_id = db_session.execute(
        sa.text("SELECT organization_id FROM users WHERE email = 't2@example.com'")
    ).scalar_one()
    org_b_id = raw_org_b_id if isinstance(raw_org_b_id, uuid.UUID) else uuid.UUID(str(raw_org_b_id))

    bad_run = TaskRun(organization_id=org_b_id, task_id=uuid.UUID(task_a_id))
    db_session.add(bad_run)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_run_list_pagination_and_isolation(client: TestClient) -> None:
    headers_a = _auth_headers(client, "Org U1", "u1@example.com")
    headers_b = _auth_headers(client, "Org U2", "u2@example.com")
    task_a_id = _create_task(client, headers_a).json()["id"]
    task_b_id = _create_task(client, headers_b).json()["id"]

    for _ in range(3):
        client.post(f"/tasks/{task_a_id}/runs", headers=headers_a)
    client.post(f"/tasks/{task_b_id}/runs", headers=headers_b)

    resp = client.get(f"/tasks/{task_a_id}/runs", headers=headers_a)
    assert resp.json()["total"] == 3

    # org B cannot list org A's task runs (the task itself 404s for them)
    cross = client.get(f"/tasks/{task_a_id}/runs", headers=headers_b)
    assert cross.status_code == 404

    assert client.get(f"/tasks/{task_a_id}/runs?limit=101", headers=headers_a).status_code == 422


def test_get_single_run(client: TestClient) -> None:
    headers = _auth_headers(client, "Org V", "v@example.com")
    task_id = _create_task(client, headers).json()["id"]
    run = client.post(f"/tasks/{task_id}/runs", headers=headers).json()

    resp = client.get(f"/tasks/{task_id}/runs/{run['id']}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["id"] == run["id"]


def test_get_single_run_cross_tenant_404(client: TestClient) -> None:
    headers_a = _auth_headers(client, "Org W1", "w1@example.com")
    headers_b = _auth_headers(client, "Org W2", "w2@example.com")
    task_id = _create_task(client, headers_a).json()["id"]
    run = client.post(f"/tasks/{task_id}/runs", headers=headers_a).json()

    resp = client.get(f"/tasks/{task_id}/runs/{run['id']}", headers=headers_b)
    assert resp.status_code == 404


# --- TaskRun state invariants (DB CHECK constraints, direct insert) ---------


def _make_task(db_session, org_id) -> "uuid.UUID":
    from app.models.task import Task
    from app.models.enums import TaskType

    task = Task(organization_id=org_id, name=f"Invariant Task {uuid.uuid4()}", task_type=TaskType.SYNC)
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)
    return task.id


def _org_id_for(db_session, email: str) -> uuid.UUID:
    import sqlalchemy as sa

    raw = db_session.execute(
        sa.text("SELECT organization_id FROM users WHERE email = :email"), {"email": email}
    ).scalar_one()
    return raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))


def test_pending_with_started_at_rejected(client: TestClient, db_session) -> None:
    from app.models.task_run import TaskRun
    from app.models.enums import TaskRunStatus

    _auth_headers(client, "Org X1", "x1@example.com")
    org_id = _org_id_for(db_session, "x1@example.com")
    task_id = _make_task(db_session, org_id)

    run = TaskRun(
        organization_id=org_id, task_id=task_id, status=TaskRunStatus.PENDING,
        started_at=datetime.now(timezone.utc),
    )
    db_session.add(run)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_running_with_finished_at_rejected(client: TestClient, db_session) -> None:
    from app.models.task_run import TaskRun
    from app.models.enums import TaskRunStatus

    _auth_headers(client, "Org X2", "x2@example.com")
    org_id = _org_id_for(db_session, "x2@example.com")
    task_id = _make_task(db_session, org_id)

    run = TaskRun(
        organization_id=org_id, task_id=task_id, status=TaskRunStatus.RUNNING,
        started_at=datetime.now(timezone.utc), finished_at=datetime.now(timezone.utc),
    )
    db_session.add(run)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_success_with_null_finished_at_rejected(client: TestClient, db_session) -> None:
    from app.models.task_run import TaskRun
    from app.models.enums import TaskRunStatus

    _auth_headers(client, "Org X3", "x3@example.com")
    org_id = _org_id_for(db_session, "x3@example.com")
    task_id = _make_task(db_session, org_id)

    run = TaskRun(
        organization_id=org_id, task_id=task_id, status=TaskRunStatus.SUCCESS,
        started_at=datetime.now(timezone.utc), finished_at=None,
    )
    db_session.add(run)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_success_with_error_message_rejected(client: TestClient, db_session) -> None:
    from app.models.task_run import TaskRun
    from app.models.enums import TaskRunStatus

    _auth_headers(client, "Org X4", "x4@example.com")
    org_id = _org_id_for(db_session, "x4@example.com")
    task_id = _make_task(db_session, org_id)
    now = datetime.now(timezone.utc)

    run = TaskRun(
        organization_id=org_id, task_id=task_id, status=TaskRunStatus.SUCCESS,
        started_at=now, finished_at=now, error_message="should not be here",
    )
    db_session.add(run)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_failed_without_error_message_rejected(client: TestClient, db_session) -> None:
    from app.models.task_run import TaskRun
    from app.models.enums import TaskRunStatus

    _auth_headers(client, "Org X5", "x5@example.com")
    org_id = _org_id_for(db_session, "x5@example.com")
    task_id = _make_task(db_session, org_id)
    now = datetime.now(timezone.utc)

    run = TaskRun(
        organization_id=org_id, task_id=task_id, status=TaskRunStatus.FAILED,
        started_at=now, finished_at=now, error_message=None,
    )
    db_session.add(run)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_finished_before_started_rejected(client: TestClient, db_session) -> None:
    from app.models.task_run import TaskRun
    from app.models.enums import TaskRunStatus

    _auth_headers(client, "Org X6", "x6@example.com")
    org_id = _org_id_for(db_session, "x6@example.com")
    task_id = _make_task(db_session, org_id)
    now = datetime.now(timezone.utc)

    run = TaskRun(
        organization_id=org_id, task_id=task_id, status=TaskRunStatus.SUCCESS,
        started_at=now, finished_at=now - timedelta(seconds=5),
    )
    db_session.add(run)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_valid_success_and_failed_rows_accepted(client: TestClient, db_session) -> None:
    from app.models.task_run import TaskRun
    from app.models.enums import TaskRunStatus

    _auth_headers(client, "Org X7", "x7@example.com")
    org_id = _org_id_for(db_session, "x7@example.com")
    task_id = _make_task(db_session, org_id)
    now = datetime.now(timezone.utc)

    success_run = TaskRun(
        organization_id=org_id, task_id=task_id, status=TaskRunStatus.SUCCESS,
        started_at=now, finished_at=now + timedelta(seconds=10),
    )
    failed_run = TaskRun(
        organization_id=org_id, task_id=task_id, status=TaskRunStatus.FAILED,
        started_at=now, finished_at=now + timedelta(seconds=10), error_message="boom",
    )
    db_session.add_all([success_run, failed_run])
    db_session.commit()  # must not raise
