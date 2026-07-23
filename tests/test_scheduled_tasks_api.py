"""Module 12: API/schema tests for scheduled task execution -- create,
update (including explicit-null vs omitted PATCH semantics), Task.schedule
deprecation (never activates anything), interval bounds, task-type gating,
and manual-run independence from next_run_at."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.core.config import get_settings


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


def _parse(iso_str: str) -> datetime:
    """SQLite (sandbox only) round-trips DateTime(timezone=True) values
    without tzinfo even though they were written as UTC-aware -- the same
    documented gotcha app.worker.engine._ensure_aware() exists to handle
    at the application layer. Tests must normalize the same way."""
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _create_task(client: TestClient, headers: dict, **overrides) -> dict:
    payload = {"name": "Nightly Sync", "task_type": "sync"}
    payload.update(overrides)
    return client.post("/tasks", json=payload, headers=headers)


# --- Create ------------------------------------------------------------


def test_create_sync_task_without_schedule_interval(client: TestClient) -> None:
    headers = _auth_headers(client, "Org Sched A", "sched-a@example.com")
    resp = _create_task(client, headers)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["schedule_interval_seconds"] is None
    assert body["next_run_at"] is None


def test_create_sync_task_with_valid_interval(client: TestClient) -> None:
    headers = _auth_headers(client, "Org Sched B", "sched-b@example.com")
    before = datetime.now(timezone.utc)
    resp = _create_task(client, headers, schedule_interval_seconds=3600)
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["schedule_interval_seconds"] == 3600
    assert body["next_run_at"] is not None
    next_run_at = _parse(body["next_run_at"])
    # next_run_at is anchored to "now" (creation time) + interval, not to
    # any other value.
    assert before + timedelta(seconds=3590) <= next_run_at <= before + timedelta(seconds=3610)


def test_task_schedule_alone_does_not_activate_scheduling(client: TestClient) -> None:
    """Task.schedule is deprecated, free-text, and non-executable -- setting
    it alone must never produce a next_run_at."""
    headers = _auth_headers(client, "Org Sched C", "sched-c@example.com")
    resp = _create_task(client, headers, schedule="every night at 2am")
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["schedule"] == "every night at 2am"
    assert body["schedule_interval_seconds"] is None
    assert body["next_run_at"] is None


def test_create_task_with_both_schedule_and_interval(client: TestClient) -> None:
    """Both fields coexist independently -- schedule remains purely
    descriptive even when schedule_interval_seconds is also set."""
    headers = _auth_headers(client, "Org Sched D", "sched-d@example.com")
    resp = _create_task(
        client, headers, schedule="nightly", schedule_interval_seconds=86400
    )
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["schedule"] == "nightly"
    assert body["schedule_interval_seconds"] == 86400
    assert body["next_run_at"] is not None


def test_create_task_interval_below_configured_minimum_rejected(client: TestClient) -> None:
    headers = _auth_headers(client, "Org Sched E", "sched-e@example.com")
    minimum = get_settings().minimum_schedule_interval_seconds
    resp = _create_task(client, headers, schedule_interval_seconds=minimum - 1)
    assert resp.status_code == 422, resp.text


def test_create_task_interval_at_configured_minimum_accepted(client: TestClient) -> None:
    headers = _auth_headers(client, "Org Sched F", "sched-f@example.com")
    minimum = get_settings().minimum_schedule_interval_seconds
    resp = _create_task(client, headers, schedule_interval_seconds=minimum)
    assert resp.status_code == 201, resp.text


def test_create_task_interval_above_configured_maximum_rejected(client: TestClient) -> None:
    headers = _auth_headers(client, "Org Sched G", "sched-g@example.com")
    maximum = get_settings().maximum_schedule_interval_seconds
    resp = _create_task(client, headers, schedule_interval_seconds=maximum + 1)
    assert resp.status_code == 422, resp.text


def test_create_non_sync_task_with_interval_rejected(client: TestClient) -> None:
    headers = _auth_headers(client, "Org Sched H", "sched-h@example.com")
    resp = _create_task(
        client, headers, task_type="transform", schedule_interval_seconds=3600
    )
    assert resp.status_code == 400, resp.text


def test_create_other_task_type_with_interval_rejected(client: TestClient) -> None:
    headers = _auth_headers(client, "Org Sched I", "sched-i@example.com")
    resp = _create_task(
        client, headers, task_type="other", schedule_interval_seconds=3600
    )
    assert resp.status_code == 400, resp.text


def test_task_read_exposes_schedule_interval_seconds_and_next_run_at(client: TestClient) -> None:
    headers = _auth_headers(client, "Org Sched J", "sched-j@example.com")
    created = _create_task(client, headers, schedule_interval_seconds=120).json()
    resp = client.get(f"/tasks/{created['id']}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert "schedule_interval_seconds" in body
    assert "next_run_at" in body
    assert body["schedule_interval_seconds"] == 120


# --- Update (PATCH) ------------------------------------------------------


def test_patch_omitted_schedule_interval_leaves_it_unchanged(client: TestClient) -> None:
    headers = _auth_headers(client, "Org Sched K", "sched-k@example.com")
    created = _create_task(client, headers, schedule_interval_seconds=300).json()

    resp = client.patch(
        f"/tasks/{created['id']}", json={"description": "updated"}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schedule_interval_seconds"] == 300
    assert body["next_run_at"] == created["next_run_at"]


def test_patch_explicit_null_clears_schedule_interval_and_next_run_at(client: TestClient) -> None:
    headers = _auth_headers(client, "Org Sched L", "sched-l@example.com")
    created = _create_task(client, headers, schedule_interval_seconds=300).json()

    resp = client.patch(
        f"/tasks/{created['id']}", json={"schedule_interval_seconds": None}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schedule_interval_seconds"] is None
    assert body["next_run_at"] is None


def test_patch_new_interval_added_computes_next_run_at(client: TestClient) -> None:
    headers = _auth_headers(client, "Org Sched M", "sched-m@example.com")
    created = _create_task(client, headers).json()
    assert created["next_run_at"] is None

    before = datetime.now(timezone.utc)
    resp = client.patch(
        f"/tasks/{created['id']}", json={"schedule_interval_seconds": 600}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schedule_interval_seconds"] == 600
    next_run_at = _parse(body["next_run_at"])
    assert before + timedelta(seconds=590) <= next_run_at <= before + timedelta(seconds=610)


def test_patch_interval_change_recomputes_next_run_at_from_now(client: TestClient) -> None:
    headers = _auth_headers(client, "Org Sched N", "sched-n@example.com")
    created = _create_task(client, headers, schedule_interval_seconds=3600).json()
    original_next_run_at = created["next_run_at"]

    before = datetime.now(timezone.utc)
    resp = client.patch(
        f"/tasks/{created['id']}", json={"schedule_interval_seconds": 60}, headers=headers
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["schedule_interval_seconds"] == 60
    next_run_at = _parse(body["next_run_at"])
    # Recomputed fresh from "now", not proportionally adjusted from the old
    # 3600s-out value -- should be far sooner than the original.
    assert next_run_at < _parse(original_next_run_at)
    assert before + timedelta(seconds=50) <= next_run_at <= before + timedelta(seconds=70)


def test_patch_changing_task_type_away_from_sync_with_active_schedule_rejected(
    client: TestClient,
) -> None:
    """Changing task_type without touching schedule_interval_seconds must
    still be rejected if it would leave a scheduled non-SYNC task --
    otherwise the SYNC-only invariant could be silently violated."""
    headers = _auth_headers(client, "Org Sched O", "sched-o@example.com")
    created = _create_task(client, headers, schedule_interval_seconds=300).json()

    resp = client.patch(
        f"/tasks/{created['id']}", json={"task_type": "transform"}, headers=headers
    )
    assert resp.status_code == 400, resp.text


def test_patch_interval_below_configured_minimum_rejected(client: TestClient) -> None:
    headers = _auth_headers(client, "Org Sched P", "sched-p@example.com")
    created = _create_task(client, headers).json()
    minimum = get_settings().minimum_schedule_interval_seconds
    resp = client.patch(
        f"/tasks/{created['id']}",
        json={"schedule_interval_seconds": minimum - 1},
        headers=headers,
    )
    assert resp.status_code == 422, resp.text


def test_patch_non_sync_task_receiving_interval_rejected(client: TestClient) -> None:
    headers = _auth_headers(client, "Org Sched Q", "sched-q@example.com")
    created = _create_task(client, headers, task_type="other").json()
    resp = client.patch(
        f"/tasks/{created['id']}",
        json={"schedule_interval_seconds": 300},
        headers=headers,
    )
    assert resp.status_code == 400, resp.text


# --- Manual run independence --------------------------------------------


def test_manual_run_does_not_affect_next_run_at(client: TestClient) -> None:
    headers = _auth_headers(client, "Org Sched R", "sched-r@example.com")
    created = _create_task(client, headers, schedule_interval_seconds=3600).json()
    original_next_run_at = created["next_run_at"]

    run_resp = client.post(f"/tasks/{created['id']}/runs", headers=headers)
    assert run_resp.status_code == 201, run_resp.text
    assert run_resp.json()["triggered_by"] is not None  # manual: real user, never NULL

    task_resp = client.get(f"/tasks/{created['id']}", headers=headers)
    assert task_resp.json()["next_run_at"] == original_next_run_at
    assert task_resp.json()["schedule_interval_seconds"] == 3600


# --- Soft-deleted tasks ----------------------------------------------------


def test_soft_deleted_scheduled_task_stays_unscheduled_in_reads(client: TestClient) -> None:
    headers = _auth_headers(client, "Org Sched S", "sched-s@example.com")
    created = _create_task(client, headers, schedule_interval_seconds=300).json()

    del_resp = client.delete(f"/tasks/{created['id']}", headers=headers)
    assert del_resp.status_code == 204

    # Deleted tasks 404 like every other inactive resource -- confirms it's
    # excluded from normal access, consistent with the scheduler excluding
    # it too (see test_scheduled_tasks_scheduler.py).
    get_resp = client.get(f"/tasks/{created['id']}", headers=headers)
    assert get_resp.status_code == 404
