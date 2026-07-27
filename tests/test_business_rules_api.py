"""Module 21: API tests for business rules endpoints.

Scenarios:
  GET    /api/v1/business-rules/global-defaults:
    1.  Returns list (may be empty since seeds not auto-run in test DB)
    2.  401 unauthenticated

  POST   /api/v1/business-rules/rule-sets:
    3.  201 creates draft rule set
    4.  created_by_identity = user email
    5.  401 unauthenticated

  GET    /api/v1/business-rules/rule-sets:
    6.  Lists own org rule sets only (tenant isolation)
    7.  is_draft filter works
    8.  is_active filter works
    9.  401 unauthenticated

  GET    /api/v1/business-rules/rule-sets/{rule_set_id}:
   10.  200 with items list
   11.  404 not found
   12.  Tenant isolation: other org's set → 404

  POST   /api/v1/business-rules/rule-sets/{rule_set_id}/items:
   13.  201 creates item
   14.  409 on duplicate rule_key
   15.  409 on published rule set

  PUT    /api/v1/business-rules/rule-sets/{rule_set_id}/items/{item_id}:
   16.  200 updates rule_value
   17.  409 on published rule set

  DELETE /api/v1/business-rules/rule-sets/{rule_set_id}/items/{item_id}:
   18.  204 deletes item
   19.  409 on published rule set

  POST   /api/v1/business-rules/rule-sets/{rule_set_id}/publish:
   20.  200 sets is_draft=False and records published_by_identity
   21.  409 on second publish attempt

  POST   /api/v1/business-rules/rule-sets/{rule_set_id}/activate:
   22.  200 sets is_active=True
   23.  Activate deactivates previous active set in same scope
   24.  409 when trying to activate a draft

  GET    /api/v1/business-rules/runs:
   25.  200 returns list (may be empty)
   26.  Filter by pipeline_run_type
   27.  401 unauthenticated
"""
from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register(client: TestClient, suffix: str) -> dict:
    resp = client.post(
        "/auth/register",
        json={
            "organization_name": f"BR API Org {suffix}",
            "email": f"br-api-{suffix}@example.com",
            "password": "horse-battery-staple-21",
            "full_name": "BR API Tester",
        },
    )
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


BASE = "/api/v1/business-rules"


def _create_rule_set(client: TestClient, headers: dict, name: str = "Test Set", **kwargs) -> dict:
    body = {"name": name, "description": "test"}
    body.update(kwargs)
    resp = client.post(f"{BASE}/rule-sets", json=body, headers=headers)
    assert resp.status_code == 201, resp.text
    return resp.json()


def _add_item(client: TestClient, headers: dict, rule_set_id: str, rule_key: str, rule_value=3.5) -> dict:
    resp = client.post(
        f"{BASE}/rule-sets/{rule_set_id}/items",
        json={"rule_key": rule_key, "rule_type": "threshold", "rule_value": rule_value},
        headers=headers,
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


def _publish(client: TestClient, headers: dict, rule_set_id: str) -> dict:
    resp = client.post(f"{BASE}/rule-sets/{rule_set_id}/publish", headers=headers)
    assert resp.status_code == 200, resp.text
    return resp.json()


# ---------------------------------------------------------------------------
# Global defaults tests
# ---------------------------------------------------------------------------


def test_global_defaults_returns_list(client: TestClient):
    headers = _register(client, "gd1")
    resp = client.get(f"{BASE}/global-defaults", headers=headers)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_global_defaults_requires_auth(client: TestClient):
    resp = client.get(f"{BASE}/global-defaults")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Create rule set
# ---------------------------------------------------------------------------


def test_create_rule_set_201(client: TestClient):
    headers = _register(client, "crs1")
    rs = _create_rule_set(client, headers, name="My Set")
    assert rs["name"] == "My Set"
    assert rs["is_draft"] is True
    assert rs["is_active"] is False
    assert rs["version"] == 1


def test_create_rule_set_sets_created_by_identity(client: TestClient):
    email = "br-api-cbi1@example.com"
    resp = client.post(
        "/auth/register",
        json={
            "organization_name": "BR CBI Org",
            "email": email,
            "password": "horse-battery-staple-21",
            "full_name": "BR CBI Tester",
        },
    )
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}
    rs = _create_rule_set(client, headers, name="CBI Set")
    assert rs["created_by_identity"] == email


def test_create_rule_set_requires_auth(client: TestClient):
    resp = client.post(f"{BASE}/rule-sets", json={"name": "x", "description": ""})
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# List rule sets
# ---------------------------------------------------------------------------


def test_list_rule_sets_own_org_only(client: TestClient):
    h1 = _register(client, "lr1a")
    h2 = _register(client, "lr1b")
    _create_rule_set(client, h1, name="Org1 Set")
    _create_rule_set(client, h2, name="Org2 Set")

    resp = client.get(f"{BASE}/rule-sets", headers=h1)
    assert resp.status_code == 200
    names = [r["name"] for r in resp.json()]
    assert "Org1 Set" in names
    assert "Org2 Set" not in names


def test_list_rule_sets_is_draft_filter(client: TestClient):
    h = _register(client, "lrf1")
    rs = _create_rule_set(client, h, name="Draft Set")
    _publish(client, h, rs["id"])

    resp_draft = client.get(f"{BASE}/rule-sets?is_draft=true", headers=h)
    resp_pub = client.get(f"{BASE}/rule-sets?is_draft=false", headers=h)
    assert resp_draft.status_code == 200
    assert resp_pub.status_code == 200
    draft_ids = {r["id"] for r in resp_draft.json()}
    pub_ids = {r["id"] for r in resp_pub.json()}
    assert rs["id"] not in draft_ids
    assert rs["id"] in pub_ids


def test_list_rule_sets_is_active_filter(client: TestClient):
    h = _register(client, "lrf2")
    rs = _create_rule_set(client, h, name="Set for Active Filter")
    _publish(client, h, rs["id"])
    client.post(f"{BASE}/rule-sets/{rs['id']}/activate", headers=h)

    resp = client.get(f"{BASE}/rule-sets?is_active=true", headers=h)
    assert resp.status_code == 200
    active_ids = {r["id"] for r in resp.json()}
    assert rs["id"] in active_ids


def test_list_rule_sets_requires_auth(client: TestClient):
    resp = client.get(f"{BASE}/rule-sets")
    assert resp.status_code == 401


# ---------------------------------------------------------------------------
# Get rule set detail
# ---------------------------------------------------------------------------


def test_get_rule_set_200_with_items(client: TestClient):
    h = _register(client, "grsd1")
    rs = _create_rule_set(client, h, name="Detail Set")
    _add_item(client, h, rs["id"], "detection.outlier_zscore_threshold", 2.5)

    resp = client.get(f"{BASE}/rule-sets/{rs['id']}", headers=h)
    assert resp.status_code == 200
    data = resp.json()
    assert data["id"] == rs["id"]
    assert len(data["items"]) == 1
    assert data["items"][0]["rule_key"] == "detection.outlier_zscore_threshold"


def test_get_rule_set_404(client: TestClient):
    h = _register(client, "grsd2")
    resp = client.get(f"{BASE}/rule-sets/{uuid.uuid4()}", headers=h)
    assert resp.status_code == 404


def test_get_rule_set_tenant_isolation(client: TestClient):
    h1 = _register(client, "grsti1")
    h2 = _register(client, "grsti2")
    rs = _create_rule_set(client, h1, name="Org1 Set Isolation")
    resp = client.get(f"{BASE}/rule-sets/{rs['id']}", headers=h2)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Items CRUD
# ---------------------------------------------------------------------------


def test_add_item_201(client: TestClient):
    h = _register(client, "ai1")
    rs = _create_rule_set(client, h, name="Item Add Set")
    item = _add_item(client, h, rs["id"], "detection.outlier_zscore_threshold", 4.0)
    assert item["rule_key"] == "detection.outlier_zscore_threshold"
    assert item["rule_value"] == 4.0
    assert item["rule_type"] == "threshold"


def test_add_item_409_duplicate_key(client: TestClient):
    h = _register(client, "ai2")
    rs = _create_rule_set(client, h, name="Dupe Key Set")
    _add_item(client, h, rs["id"], "detection.outlier_zscore_threshold", 3.0)
    resp = client.post(
        f"{BASE}/rule-sets/{rs['id']}/items",
        json={"rule_key": "detection.outlier_zscore_threshold", "rule_type": "threshold", "rule_value": 5.0},
        headers=h,
    )
    assert resp.status_code == 409


def test_add_item_409_published_set(client: TestClient):
    h = _register(client, "ai3")
    rs = _create_rule_set(client, h, name="Pub Set No Mod")
    _publish(client, h, rs["id"])
    resp = client.post(
        f"{BASE}/rule-sets/{rs['id']}/items",
        json={"rule_key": "detection.outlier_zscore_threshold", "rule_type": "threshold", "rule_value": 5.0},
        headers=h,
    )
    assert resp.status_code == 409


def test_update_item_200(client: TestClient):
    h = _register(client, "ui1")
    rs = _create_rule_set(client, h, name="Update Item Set")
    item = _add_item(client, h, rs["id"], "detection.outlier_zscore_threshold", 3.0)

    resp = client.put(
        f"{BASE}/rule-sets/{rs['id']}/items/{item['id']}",
        json={"rule_value": 7.0},
        headers=h,
    )
    assert resp.status_code == 200
    assert resp.json()["rule_value"] == 7.0


def test_update_item_409_published(client: TestClient):
    h = _register(client, "ui2")
    rs = _create_rule_set(client, h, name="Update Published Set")
    item = _add_item(client, h, rs["id"], "detection.outlier_zscore_threshold", 3.0)
    _publish(client, h, rs["id"])

    resp = client.put(
        f"{BASE}/rule-sets/{rs['id']}/items/{item['id']}",
        json={"rule_value": 9.0},
        headers=h,
    )
    assert resp.status_code == 409


def test_delete_item_204(client: TestClient):
    h = _register(client, "di1")
    rs = _create_rule_set(client, h, name="Delete Item Set")
    item = _add_item(client, h, rs["id"], "detection.outlier_zscore_threshold", 3.0)

    resp = client.delete(f"{BASE}/rule-sets/{rs['id']}/items/{item['id']}", headers=h)
    assert resp.status_code == 204

    # Verify item is gone
    detail = client.get(f"{BASE}/rule-sets/{rs['id']}", headers=h).json()
    assert len(detail["items"]) == 0


def test_delete_item_409_published(client: TestClient):
    h = _register(client, "di2")
    rs = _create_rule_set(client, h, name="Delete Published Item Set")
    item = _add_item(client, h, rs["id"], "detection.outlier_zscore_threshold", 3.0)
    _publish(client, h, rs["id"])

    resp = client.delete(f"{BASE}/rule-sets/{rs['id']}/items/{item['id']}", headers=h)
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Publish
# ---------------------------------------------------------------------------


def test_publish_rule_set_200(client: TestClient):
    h = _register(client, "pub1")
    rs = _create_rule_set(client, h, name="Publish Set")
    resp = client.post(f"{BASE}/rule-sets/{rs['id']}/publish", headers=h)
    assert resp.status_code == 200
    data = resp.json()
    assert data["is_draft"] is False
    assert data["published_at"] is not None
    assert data["published_by_identity"] is not None


def test_publish_rule_set_409_already_published(client: TestClient):
    h = _register(client, "pub2")
    rs = _create_rule_set(client, h, name="Double Publish Set")
    _publish(client, h, rs["id"])
    resp = client.post(f"{BASE}/rule-sets/{rs['id']}/publish", headers=h)
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Activate
# ---------------------------------------------------------------------------


def test_activate_rule_set_200(client: TestClient):
    h = _register(client, "act1")
    rs = _create_rule_set(client, h, name="Activate Set")
    _publish(client, h, rs["id"])
    resp = client.post(f"{BASE}/rule-sets/{rs['id']}/activate", headers=h)
    assert resp.status_code == 200
    assert resp.json()["is_active"] is True


def test_activate_deactivates_previous(client: TestClient):
    h = _register(client, "act2")
    rs1 = _create_rule_set(client, h, name="Active Set 1")
    rs2 = _create_rule_set(client, h, name="Active Set 2")
    _publish(client, h, rs1["id"])
    _publish(client, h, rs2["id"])
    client.post(f"{BASE}/rule-sets/{rs1['id']}/activate", headers=h)
    client.post(f"{BASE}/rule-sets/{rs2['id']}/activate", headers=h)

    # rs1 should now be inactive
    detail1 = client.get(f"{BASE}/rule-sets/{rs1['id']}", headers=h).json()
    detail2 = client.get(f"{BASE}/rule-sets/{rs2['id']}", headers=h).json()
    assert detail1["is_active"] is False
    assert detail2["is_active"] is True


def test_activate_draft_returns_409(client: TestClient):
    h = _register(client, "act3")
    rs = _create_rule_set(client, h, name="Draft Activate Set")
    resp = client.post(f"{BASE}/rule-sets/{rs['id']}/activate", headers=h)
    assert resp.status_code == 409


# ---------------------------------------------------------------------------
# Runs
# ---------------------------------------------------------------------------


def test_list_runs_200(client: TestClient):
    h = _register(client, "runs1")
    resp = client.get(f"{BASE}/runs", headers=h)
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)


def test_list_runs_invalid_type_422(client: TestClient):
    h = _register(client, "runs2")
    resp = client.get(f"{BASE}/runs?pipeline_run_type=bogus_type", headers=h)
    assert resp.status_code == 422


def test_list_runs_requires_auth(client: TestClient):
    resp = client.get(f"{BASE}/runs")
    assert resp.status_code == 401
