"""Tests for /data-sources: CRUD, secrets rejection, case-insensitive
uniqueness, soft-delete semantics, pagination, and tenant isolation."""
import uuid

from fastapi.testclient import TestClient


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
        "connection_metadata": {"host": "db.internal", "port": 5432},
    }
    payload.update(overrides)
    resp = client.post("/data-sources", json=payload, headers=headers)
    return resp


# --- Create --------------------------------------------------------------


def test_create_data_source_success(client: TestClient) -> None:
    headers = _auth_headers(client, "Org A", "a@example.com")
    resp = _create_source(client, headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["name"] == "Primary Postgres"
    assert body["source_type"] == "postgres"
    assert body["connection_metadata"] == {"host": "db.internal", "port": 5432}
    assert body["is_active"] is True


def test_create_rejects_secret_like_key(client: TestClient) -> None:
    headers = _auth_headers(client, "Org B", "b@example.com")
    resp = _create_source(client, headers, connection_metadata={"password": "hunter2"})
    assert resp.status_code == 422

    resp2 = _create_source(
        client, headers, name="Other", connection_metadata={"auth": {"api_key": "x"}}
    )
    assert resp2.status_code == 422


def test_create_rejects_invalid_source_type(client: TestClient) -> None:
    headers = _auth_headers(client, "Org C", "c@example.com")
    resp = _create_source(client, headers, source_type="not_a_real_type")
    assert resp.status_code == 422


def test_create_rejects_client_supplied_organization_id_and_created_by(
    client: TestClient,
) -> None:
    headers = _auth_headers(client, "Org D", "d@example.com")
    resp = client.post(
        "/data-sources",
        json={
            "name": "X",
            "source_type": "postgres",
            "connection_metadata": {},
            "organization_id": str(uuid.uuid4()),
            "created_by": str(uuid.uuid4()),
        },
        headers=headers,
    )
    assert resp.status_code == 422


def test_created_by_is_always_current_user(client: TestClient, db_session) -> None:
    from app.models.data_source import DataSource
    from app.models.user import User

    headers = _auth_headers(client, "Org E", "e@example.com")
    resp = _create_source(client, headers)
    data_source_id = resp.json()["id"]

    ds = db_session.get(DataSource, uuid.UUID(data_source_id))
    user = db_session.query(User).filter(User.email == "e@example.com").one()
    assert ds.created_by == user.id


def test_create_unauthenticated_rejected(client: TestClient) -> None:
    resp = client.post(
        "/data-sources",
        json={"name": "X", "source_type": "postgres", "connection_metadata": {}},
    )
    assert resp.status_code == 401


# --- Name uniqueness (case-insensitive, per-org) --------------------------


def test_case_insensitive_name_collision_rejected(client: TestClient) -> None:
    headers = _auth_headers(client, "Org F", "f@example.com")
    r1 = _create_source(client, headers, name="Acme DB")
    assert r1.status_code == 201

    r2 = _create_source(client, headers, name="  acme db  ")
    assert r2.status_code == 409


def test_name_reusable_after_soft_delete(client: TestClient) -> None:
    headers = _auth_headers(client, "Org G", "g@example.com")
    r1 = _create_source(client, headers, name="Reusable Name")
    assert r1.status_code == 201
    ds_id = r1.json()["id"]

    del_resp = client.delete(f"/data-sources/{ds_id}", headers=headers)
    assert del_resp.status_code == 204

    r2 = _create_source(client, headers, name="Reusable Name")
    assert r2.status_code == 201


# --- Pagination ------------------------------------------------------------


def test_list_pagination_defaults_and_max(client: TestClient) -> None:
    headers = _auth_headers(client, "Org H", "h@example.com")
    for i in range(3):
        _create_source(client, headers, name=f"Source {i}")

    resp = client.get("/data-sources", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["limit"] == 50
    assert body["offset"] == 0
    assert body["total"] == 3
    assert len(body["items"]) == 3

    too_big = client.get("/data-sources?limit=101", headers=headers)
    assert too_big.status_code == 422

    at_max = client.get("/data-sources?limit=100", headers=headers)
    assert at_max.status_code == 200


def test_list_excludes_inactive_by_default(client: TestClient) -> None:
    headers = _auth_headers(client, "Org I", "i@example.com")
    r1 = _create_source(client, headers, name="Will Delete")
    ds_id = r1.json()["id"]
    client.delete(f"/data-sources/{ds_id}", headers=headers)

    resp = client.get("/data-sources", headers=headers)
    assert resp.json()["total"] == 0

    resp_incl = client.get("/data-sources?include_inactive=true", headers=headers)
    assert resp_incl.json()["total"] == 1


# --- Soft-delete semantics ---------------------------------------------------


def test_get_patch_delete_on_inactive_returns_404(client: TestClient) -> None:
    headers = _auth_headers(client, "Org J", "j@example.com")
    r1 = _create_source(client, headers, name="Soon Inactive")
    ds_id = r1.json()["id"]
    assert client.delete(f"/data-sources/{ds_id}", headers=headers).status_code == 204

    assert client.get(f"/data-sources/{ds_id}", headers=headers).status_code == 404
    assert client.patch(
        f"/data-sources/{ds_id}", json={"name": "New Name"}, headers=headers
    ).status_code == 404
    assert client.delete(f"/data-sources/{ds_id}", headers=headers).status_code == 404


def test_delete_sets_is_active_false(client: TestClient, db_session) -> None:
    from app.models.data_source import DataSource

    headers = _auth_headers(client, "Org K", "k@example.com")
    r1 = _create_source(client, headers, name="To Delete")
    ds_id = r1.json()["id"]
    client.delete(f"/data-sources/{ds_id}", headers=headers)

    ds = db_session.get(DataSource, uuid.UUID(ds_id))
    assert ds.is_active is False


# --- Tenant isolation --------------------------------------------------------


def test_cross_tenant_get_patch_delete_returns_404(client: TestClient) -> None:
    headers_a = _auth_headers(client, "Org L1", "l1@example.com")
    headers_b = _auth_headers(client, "Org L2", "l2@example.com")

    r1 = _create_source(client, headers_a, name="Org A Source")
    ds_id = r1.json()["id"]

    assert client.get(f"/data-sources/{ds_id}", headers=headers_b).status_code == 404
    assert client.patch(
        f"/data-sources/{ds_id}", json={"name": "Hijacked"}, headers=headers_b
    ).status_code == 404
    assert client.delete(f"/data-sources/{ds_id}", headers=headers_b).status_code == 404

    # Confirm org A can still access it (proves the 404 above was isolation,
    # not a bug that broke the resource entirely).
    assert client.get(f"/data-sources/{ds_id}", headers=headers_a).status_code == 200


def test_list_only_shows_own_org_data_sources(client: TestClient) -> None:
    headers_a = _auth_headers(client, "Org M1", "m1@example.com")
    headers_b = _auth_headers(client, "Org M2", "m2@example.com")
    _create_source(client, headers_a, name="A Source")
    _create_source(client, headers_b, name="B Source")

    resp_a = client.get("/data-sources", headers=headers_a)
    names_a = {item["name"] for item in resp_a.json()["items"]}
    assert names_a == {"A Source"}
