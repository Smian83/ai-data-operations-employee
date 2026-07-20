"""HTTP-level tests for the Module 4 API surface:
- PUT /data-sources/{id}/credentials (write-only, tenant-scoped, 404 on
  inactive/cross-org)
- GET /tasks/{id}/runs/{run_id}/events (read-only audit trail)
- GET /internal/metrics (superuser-gated)

Confirms the "public API clients must not directly change TaskRun status"
requirement holds: there is no PATCH/PUT for task runs anywhere in this
router, Module 4 added none, and status only ever changes via the engine.
"""
import uuid

from fastapi.testclient import TestClient

from app.core.security import hash_password
from app.models.organization import Organization
from app.models.user import User
from app.worker.engine import claim_batch, complete_success


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


# --- Credentials write-only endpoint ------------------------------------------


def test_set_credentials_success(client: TestClient) -> None:
    headers = _auth_headers(client, "Org API A", "api-a@example.com")
    ds = client.post(
        "/data-sources",
        json={"name": "Warehouse", "source_type": "postgres", "connection_metadata": {}},
        headers=headers,
    ).json()

    resp = client.put(
        f"/data-sources/{ds['id']}/credentials",
        json={"credentials": {"username": "u", "password": "p"}},
        headers=headers,
    )
    assert resp.status_code == 204
    assert resp.text == ""


def test_set_credentials_rejects_empty_body(client: TestClient) -> None:
    headers = _auth_headers(client, "Org API B", "api-b@example.com")
    ds = client.post(
        "/data-sources",
        json={"name": "Warehouse", "source_type": "postgres", "connection_metadata": {}},
        headers=headers,
    ).json()
    resp = client.put(
        f"/data-sources/{ds['id']}/credentials", json={"credentials": {}}, headers=headers
    )
    assert resp.status_code == 422


def test_set_credentials_on_cross_org_data_source_404(client: TestClient) -> None:
    headers_a = _auth_headers(client, "Org API C1", "api-c1@example.com")
    headers_b = _auth_headers(client, "Org API C2", "api-c2@example.com")
    ds = client.post(
        "/data-sources",
        json={"name": "Warehouse", "source_type": "postgres", "connection_metadata": {}},
        headers=headers_a,
    ).json()

    resp = client.put(
        f"/data-sources/{ds['id']}/credentials",
        json={"credentials": {"password": "p"}},
        headers=headers_b,
    )
    assert resp.status_code == 404


def test_set_credentials_on_inactive_data_source_404(client: TestClient) -> None:
    headers = _auth_headers(client, "Org API D", "api-d@example.com")
    ds = client.post(
        "/data-sources",
        json={"name": "Warehouse", "source_type": "postgres", "connection_metadata": {}},
        headers=headers,
    ).json()
    client.delete(f"/data-sources/{ds['id']}", headers=headers)

    resp = client.put(
        f"/data-sources/{ds['id']}/credentials",
        json={"credentials": {"password": "p"}},
        headers=headers,
    )
    assert resp.status_code == 404


def test_no_get_endpoint_exists_for_credentials(client: TestClient) -> None:
    headers = _auth_headers(client, "Org API E", "api-e@example.com")
    ds = client.post(
        "/data-sources",
        json={"name": "Warehouse", "source_type": "postgres", "connection_metadata": {}},
        headers=headers,
    ).json()
    resp = client.get(f"/data-sources/{ds['id']}/credentials", headers=headers)
    assert resp.status_code in (404, 405)  # no such route


# --- TaskRun status is never client-mutable -----------------------------------


def test_no_mutation_endpoint_exists_for_task_run_status(client: TestClient) -> None:
    headers = _auth_headers(client, "Org API F", "api-f@example.com")
    task = client.post("/tasks", json={"name": "T", "task_type": "sync"}, headers=headers).json()
    run = client.post(f"/tasks/{task['id']}/runs", headers=headers).json()

    patch_resp = client.patch(
        f"/tasks/{task['id']}/runs/{run['id']}", json={"status": "success"}, headers=headers
    )
    assert patch_resp.status_code in (404, 405)


# --- Task run audit events -----------------------------------------------------


def test_task_run_events_empty_before_execution(client: TestClient) -> None:
    headers = _auth_headers(client, "Org API G", "api-g@example.com")
    task = client.post("/tasks", json={"name": "T", "task_type": "sync"}, headers=headers).json()
    run = client.post(f"/tasks/{task['id']}/runs", headers=headers).json()

    resp = client.get(f"/tasks/{task['id']}/runs/{run['id']}/events", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["total"] == 0


def test_task_run_events_populated_after_engine_activity(client: TestClient, db_session) -> None:
    headers = _auth_headers(client, "Org API H", "api-h@example.com")
    task = client.post("/tasks", json={"name": "T", "task_type": "sync"}, headers=headers).json()
    run = client.post(f"/tasks/{task['id']}/runs", headers=headers).json()

    claimed = claim_batch(db_session, worker_id="w1")
    complete_success(db_session, claimed[0].id, claimed[0].lease_token, worker_id="w1")

    resp = client.get(f"/tasks/{task['id']}/runs/{run['id']}/events", headers=headers)
    event_types = [e["event_type"] for e in resp.json()["items"]]
    assert "claimed" in event_types
    assert "succeeded" in event_types


def test_task_run_events_cross_org_404(client: TestClient) -> None:
    headers_a = _auth_headers(client, "Org API I1", "api-i1@example.com")
    headers_b = _auth_headers(client, "Org API I2", "api-i2@example.com")
    task = client.post("/tasks", json={"name": "T", "task_type": "sync"}, headers=headers_a).json()
    run = client.post(f"/tasks/{task['id']}/runs", headers=headers_a).json()

    resp = client.get(f"/tasks/{task['id']}/runs/{run['id']}/events", headers=headers_b)
    assert resp.status_code == 404


# --- Internal metrics endpoint -------------------------------------------------


def test_metrics_requires_authentication(client: TestClient) -> None:
    resp = client.get("/internal/metrics")
    assert resp.status_code == 401


def test_metrics_accessible_to_superuser(client: TestClient) -> None:
    # The first user of a newly registered org is always a superuser
    # (app.api.auth.register), so the standard test helper already covers
    # the allowed path.
    headers = _auth_headers(client, "Org API J", "api-j@example.com")
    resp = client.get("/internal/metrics", headers=headers)
    assert resp.status_code == 200
    assert "task_engine_queue_depth" in resp.text


def test_metrics_rejected_for_non_superuser(client: TestClient, db_session) -> None:
    headers = _auth_headers(client, "Org API K", "api-k@example.com")
    org = db_session.query(Organization).filter(Organization.name == "Org API K").one()

    non_admin = User(
        organization_id=org.id,
        email="non-admin@example.com",
        hashed_password=hash_password("another-horse-battery"),
        is_superuser=False,
    )
    db_session.add(non_admin)
    db_session.commit()

    login_resp = client.post(
        "/auth/login",
        json={
            "organization_slug": org.slug,
            "email": "non-admin@example.com",
            "password": "another-horse-battery",
        },
    )
    assert login_resp.status_code == 200
    non_admin_headers = {"Authorization": f"Bearer {login_resp.json()['access_token']}"}

    resp = client.get("/internal/metrics", headers=non_admin_headers)
    assert resp.status_code == 403
