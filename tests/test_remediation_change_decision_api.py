"""Module 16 Phase 2 tests: HTTP API coverage for the three approval-queue
endpoints.

  POST /{task_id}/runs/{run_id}/remediation/changes/{change_id}/approve  201
  POST /{task_id}/runs/{run_id}/remediation/changes/{change_id}/reject   201
  GET  /{task_id}/runs/{run_id}/remediation/changes/{change_id}/decision 200

Sections:
  A. Happy-path: approve
  B. Happy-path: reject
  C. reviewer_name fallback (full_name → email)
  D. reviewer_role is always "superuser"
  E. applied_at / applied_by always NULL in Module 16
  F. 403: non-superuser cannot write decisions
  G. 404: wrong task_id / run_id / change_id
  H. 409: duplicate decision (approve-then-approve, reject-then-approve, etc.)
  I. GET /decision: happy path
  J. GET /decision: 404 when no decision exists yet
  K. GET /decision: accessible to non-superuser (read is org-wide)
  L. Cross-org isolation: cannot read or write another org's decisions
"""
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.models.issue import Issue
from app.models.issue_detection_run import IssueDetectionRun
from app.models.remediation_change import RemediationChange
from app.models.remediation_run import RemediationRun
from app.models.task import Task
from app.models.user import User as UserModel


# ---------------------------------------------------------------------------
# Fixture: full prerequisite chain
# ---------------------------------------------------------------------------


def _build_chain(client: TestClient, db_session, suffix: str | None = None):
    """Register an org + superuser, build a complete DETECT → REMEDIATE
    chain, and return the IDs needed for the decision endpoints.

    Returns a dict with keys:
        superuser_headers, org_id, user_id,
        remediate_task_id, remediate_task_run_id, change_id

    `remediate_task_run_id` is the TaskRun.id (the `run_id` URL parameter),
    NOT RemediationRun.id.
    """
    suffix = suffix or uuid.uuid4().hex[:8]

    # --- Register superuser (first user = superuser) ---
    reg = client.post(
        "/auth/register",
        json={
            "organization_name": f"Decision API Org {suffix}",
            "email": f"decision-su-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Super Reviewer",
        },
    )
    assert reg.status_code == 201, reg.text
    su_headers = {"Authorization": f"Bearer {reg.json()['access_token']}"}

    # Fetch org_id + user_id from DB
    user_row = db_session.execute(
        __import__("sqlalchemy", fromlist=["select"]).select(UserModel).where(
            UserModel.email == f"decision-su-{suffix}@example.com"
        )
    ).scalar_one()
    org_id = user_row.organization_id
    user_id = user_row.id

    # --- Data source ---
    src = client.post(
        "/data-sources",
        json={
            "name": f"DS {suffix}",
            "source_type": "csv_upload",
            "connection_metadata": {"file_path": "data.csv"},
        },
        headers=su_headers,
    )
    assert src.status_code == 201, src.text
    source_id = src.json()["id"]

    # --- DETECT task + TaskRun ---
    dt = client.post(
        "/tasks",
        json={"name": f"Detect {suffix}", "task_type": "detect", "data_source_id": source_id},
        headers=su_headers,
    )
    assert dt.status_code == 201, dt.text
    detect_task_id = dt.json()["id"]

    dr = client.post(f"/tasks/{detect_task_id}/runs", headers=su_headers)
    assert dr.status_code == 201, dr.text
    detect_run_id = uuid.UUID(dr.json()["id"])

    detect_task = db_session.get(Task, uuid.UUID(detect_task_id))

    # IssueDetectionRun + Issue (direct ORM — same as test_remediation_api.py)
    idr = IssueDetectionRun(
        organization_id=org_id,
        task_run_id=detect_run_id,
        task_id=detect_task.id,
        data_source_id=uuid.UUID(source_id),
        rows_scanned=1,
        columns_scanned=1,
        total_issues_found=1,
        persisted_issue_count=1,
        issues_by_severity={"LOW": 1},
        issues_by_type={"leading_whitespace": 1},
        limits_applied={"max_persisted_issues": 10_000},
        detection_engine_version="1.0",
        source_sha256="b" * 64,
    )
    db_session.add(idr)
    db_session.commit()
    db_session.refresh(idr)

    issue = Issue(
        organization_id=org_id,
        detection_run_id=idr.id,
        row_number=1,
        column_name="name",
        issue_type="leading_whitespace",
        severity="LOW",
        original_value="  Alice",
        suggested_fix="Alice",
        confidence=1.0,
    )
    db_session.add(issue)
    db_session.commit()
    db_session.refresh(issue)

    # --- REMEDIATE task + TaskRun ---
    rt = client.post(
        "/tasks",
        json={"name": f"Remediate {suffix}", "task_type": "remediate", "data_source_id": source_id},
        headers=su_headers,
    )
    assert rt.status_code == 201, rt.text
    remediate_task_id = rt.json()["id"]

    rr_resp = client.post(
        f"/tasks/{remediate_task_id}/runs",
        json={"source_task_run_id": str(detect_run_id)},
        headers=su_headers,
    )
    assert rr_resp.status_code == 201, rr_resp.text
    remediate_task_run_id = uuid.UUID(rr_resp.json()["id"])  # TaskRun.id = URL run_id

    remediate_task = db_session.get(Task, uuid.UUID(remediate_task_id))

    # RemediationRun + RemediationChange (direct ORM)
    rrun = RemediationRun(
        organization_id=org_id,
        task_run_id=remediate_task_run_id,
        task_id=remediate_task.id,
        data_source_id=uuid.UUID(source_id),
        source_task_run_id=detect_run_id,
        issues_considered_count=1,
        total_changes_count=1,
        issues_skipped_count=0,
        changes_by_action={"trim_whitespace": 1},
        skipped_by_reason={},
        remediation_engine_version="1.0",
    )
    db_session.add(rrun)
    db_session.commit()
    db_session.refresh(rrun)

    rchange = RemediationChange(
        organization_id=org_id,
        remediation_run_id=rrun.id,
        source_issue_id=issue.id,
        row_number=1,
        column_name="name",
        action="trim_whitespace",
        original_value="  Alice",
        proposed_value="Alice",
        reason="leading whitespace removed",
        confidence=1.0,
    )
    db_session.add(rchange)
    db_session.commit()
    db_session.refresh(rchange)

    return {
        "superuser_headers": su_headers,
        "org_id": org_id,
        "user_id": user_id,
        "remediate_task_id": remediate_task_id,
        "remediate_task_run_id": str(remediate_task_run_id),
        "change_id": str(rchange.id),
    }


def _approve_url(chain: dict) -> str:
    return (
        f"/tasks/{chain['remediate_task_id']}"
        f"/runs/{chain['remediate_task_run_id']}"
        f"/remediation/changes/{chain['change_id']}/approve"
    )


def _reject_url(chain: dict) -> str:
    return (
        f"/tasks/{chain['remediate_task_id']}"
        f"/runs/{chain['remediate_task_run_id']}"
        f"/remediation/changes/{chain['change_id']}/reject"
    )


def _decision_url(chain: dict) -> str:
    return (
        f"/tasks/{chain['remediate_task_id']}"
        f"/runs/{chain['remediate_task_run_id']}"
        f"/remediation/changes/{chain['change_id']}/decision"
    )


# ---------------------------------------------------------------------------
# Section A: Happy-path — approve
# ---------------------------------------------------------------------------


def test_approve_returns_201_with_correct_fields(client, db_session):
    chain = _build_chain(client, db_session)
    resp = client.post(_approve_url(chain), headers=chain["superuser_headers"])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["decision"] == "approved"
    assert body["remediation_change_id"] == chain["change_id"]
    assert body["organization_id"] == str(chain["org_id"])
    assert body["reviewer_id"] == str(chain["user_id"])
    assert body["reviewer_role"] == "superuser"
    assert body["reviewer_name"] == "Super Reviewer"
    assert body["id"] is not None
    assert body["decision_timestamp"] is not None
    assert body["created_at"] is not None


def test_approve_with_comment_stores_comment(client, db_session):
    chain = _build_chain(client, db_session)
    resp = client.post(
        _approve_url(chain),
        json={"comment": "Looks good, approved."},
        headers=chain["superuser_headers"],
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["comment"] == "Looks good, approved."


def test_approve_without_body_stores_null_comment(client, db_session):
    chain = _build_chain(client, db_session)
    resp = client.post(_approve_url(chain), headers=chain["superuser_headers"])
    assert resp.status_code == 201, resp.text
    assert resp.json()["comment"] is None


# ---------------------------------------------------------------------------
# Section B: Happy-path — reject
# ---------------------------------------------------------------------------


def test_reject_returns_201_with_correct_fields(client, db_session):
    chain = _build_chain(client, db_session)
    resp = client.post(_reject_url(chain), headers=chain["superuser_headers"])
    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["decision"] == "rejected"
    assert body["remediation_change_id"] == chain["change_id"]
    assert body["reviewer_role"] == "superuser"


def test_reject_with_comment_stores_comment(client, db_session):
    chain = _build_chain(client, db_session)
    resp = client.post(
        _reject_url(chain),
        json={"comment": "Change looks risky."},
        headers=chain["superuser_headers"],
    )
    assert resp.status_code == 201, resp.text
    assert resp.json()["comment"] == "Change looks risky."


def test_reject_without_body_stores_null_comment(client, db_session):
    chain = _build_chain(client, db_session)
    resp = client.post(_reject_url(chain), headers=chain["superuser_headers"])
    assert resp.status_code == 201, resp.text
    assert resp.json()["comment"] is None


# ---------------------------------------------------------------------------
# Section C: reviewer_name fallback (full_name → email)
# ---------------------------------------------------------------------------


def test_reviewer_name_is_full_name_when_set(client, db_session):
    """Covered by default: _build_chain registers with full_name='Super Reviewer'."""
    chain = _build_chain(client, db_session)
    resp = client.post(_approve_url(chain), headers=chain["superuser_headers"])
    assert resp.status_code == 201, resp.text
    assert resp.json()["reviewer_name"] == "Super Reviewer"


def test_reviewer_name_falls_back_to_email_when_full_name_is_none(client, db_session):
    """Insert a superuser with full_name=None in the chain's org; when they
    approve, reviewer_name must be their email address (not None)."""
    from app.core.security import hash_password

    chain = _build_chain(client, db_session)
    suffix = uuid.uuid4().hex[:8]
    email = f"noname-{suffix}@example.com"

    noname_user = UserModel(
        organization_id=chain["org_id"],
        email=email,
        hashed_password=hash_password("correct-horse-battery"),
        full_name=None,
        is_superuser=True,
    )
    db_session.add(noname_user)
    db_session.commit()

    noname_headers = _login(client, db_session, chain["org_id"], email)
    resp = client.post(_approve_url(chain), headers=noname_headers)
    assert resp.status_code == 201, resp.text
    assert resp.json()["reviewer_name"] == email


# ---------------------------------------------------------------------------
# Section D: reviewer_role is always "superuser"
# ---------------------------------------------------------------------------


def test_reviewer_role_is_always_superuser_string(client, db_session):
    chain = _build_chain(client, db_session)
    resp = client.post(_approve_url(chain), headers=chain["superuser_headers"])
    assert resp.status_code == 201, resp.text
    assert resp.json()["reviewer_role"] == "superuser"


# ---------------------------------------------------------------------------
# Section E: applied_at / applied_by always NULL in Module 16
# ---------------------------------------------------------------------------


def test_applied_at_is_null_after_approve(client, db_session):
    chain = _build_chain(client, db_session)
    resp = client.post(_approve_url(chain), headers=chain["superuser_headers"])
    assert resp.status_code == 201, resp.text
    assert resp.json()["applied_at"] is None


def test_applied_by_is_null_after_approve(client, db_session):
    chain = _build_chain(client, db_session)
    resp = client.post(_approve_url(chain), headers=chain["superuser_headers"])
    assert resp.status_code == 201, resp.text
    assert resp.json()["applied_by"] is None


def test_applied_at_is_null_after_reject(client, db_session):
    chain = _build_chain(client, db_session)
    resp = client.post(_reject_url(chain), headers=chain["superuser_headers"])
    assert resp.status_code == 201, resp.text
    assert resp.json()["applied_at"] is None


def test_applied_by_is_null_after_reject(client, db_session):
    chain = _build_chain(client, db_session)
    resp = client.post(_reject_url(chain), headers=chain["superuser_headers"])
    assert resp.status_code == 201, resp.text
    assert resp.json()["applied_by"] is None


# ---------------------------------------------------------------------------
# Section F: 403 — non-superuser cannot write decisions
# ---------------------------------------------------------------------------


def _login(client: TestClient, db_session, org_id, email: str, password: str = "correct-horse-battery") -> dict:
    """Look up the org slug and log in, returning auth headers."""
    from app.models.organization import Organization

    org = db_session.get(Organization, org_id)
    assert org is not None, f"Org {org_id} not found"
    login = client.post(
        "/auth/login",
        json={"organization_slug": org.slug, "email": email, "password": password},
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


def _register_nonsuperuser_in_org(client: TestClient, db_session, org_id) -> dict:
    """Insert a non-superuser into an existing org and return auth headers."""
    from app.core.security import hash_password

    suffix = uuid.uuid4().hex[:8]
    email = f"regular-{suffix}@example.com"
    user = UserModel(
        organization_id=org_id,
        email=email,
        hashed_password=hash_password("correct-horse-battery"),
        full_name="Regular User",
        is_superuser=False,
    )
    db_session.add(user)
    db_session.commit()
    return _login(client, db_session, org_id, email)


def test_regular_user_cannot_approve(client, db_session):
    chain = _build_chain(client, db_session)
    regular_headers = _register_nonsuperuser_in_org(client, db_session, chain["org_id"])
    resp = client.post(_approve_url(chain), headers=regular_headers)
    assert resp.status_code == 403, resp.text


def test_regular_user_cannot_reject(client, db_session):
    chain = _build_chain(client, db_session)
    regular_headers = _register_nonsuperuser_in_org(client, db_session, chain["org_id"])
    resp = client.post(_reject_url(chain), headers=regular_headers)
    assert resp.status_code == 403, resp.text


def test_unauthenticated_cannot_approve(client, db_session):
    chain = _build_chain(client, db_session)
    resp = client.post(_approve_url(chain))
    assert resp.status_code == 401, resp.text


def test_unauthenticated_cannot_reject(client, db_session):
    chain = _build_chain(client, db_session)
    resp = client.post(_reject_url(chain))
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# Section G: 404 — wrong task_id / run_id / change_id
# ---------------------------------------------------------------------------


def test_approve_wrong_change_id_returns_404(client, db_session):
    chain = _build_chain(client, db_session)
    url = (
        f"/tasks/{chain['remediate_task_id']}"
        f"/runs/{chain['remediate_task_run_id']}"
        f"/remediation/changes/{uuid.uuid4()}/approve"
    )
    resp = client.post(url, headers=chain["superuser_headers"])
    assert resp.status_code == 404, resp.text


def test_approve_wrong_run_id_returns_404(client, db_session):
    chain = _build_chain(client, db_session)
    url = (
        f"/tasks/{chain['remediate_task_id']}"
        f"/runs/{uuid.uuid4()}"
        f"/remediation/changes/{chain['change_id']}/approve"
    )
    resp = client.post(url, headers=chain["superuser_headers"])
    assert resp.status_code == 404, resp.text


def test_approve_wrong_task_id_returns_404(client, db_session):
    chain = _build_chain(client, db_session)
    url = (
        f"/tasks/{uuid.uuid4()}"
        f"/runs/{chain['remediate_task_run_id']}"
        f"/remediation/changes/{chain['change_id']}/approve"
    )
    resp = client.post(url, headers=chain["superuser_headers"])
    assert resp.status_code == 404, resp.text


def test_reject_wrong_change_id_returns_404(client, db_session):
    chain = _build_chain(client, db_session)
    url = (
        f"/tasks/{chain['remediate_task_id']}"
        f"/runs/{chain['remediate_task_run_id']}"
        f"/remediation/changes/{uuid.uuid4()}/reject"
    )
    resp = client.post(url, headers=chain["superuser_headers"])
    assert resp.status_code == 404, resp.text


def test_get_decision_wrong_change_id_returns_404(client, db_session):
    chain = _build_chain(client, db_session)
    url = (
        f"/tasks/{chain['remediate_task_id']}"
        f"/runs/{chain['remediate_task_run_id']}"
        f"/remediation/changes/{uuid.uuid4()}/decision"
    )
    resp = client.get(url, headers=chain["superuser_headers"])
    assert resp.status_code == 404, resp.text


# ---------------------------------------------------------------------------
# Section H: 409 — duplicate decision
# ---------------------------------------------------------------------------


def test_second_approve_returns_409(client, db_session):
    chain = _build_chain(client, db_session)
    r1 = client.post(_approve_url(chain), headers=chain["superuser_headers"])
    assert r1.status_code == 201, r1.text
    r2 = client.post(_approve_url(chain), headers=chain["superuser_headers"])
    assert r2.status_code == 409, r2.text
    assert "decision already exists" in r2.json()["detail"].lower()


def test_reject_after_approve_returns_409(client, db_session):
    chain = _build_chain(client, db_session)
    r1 = client.post(_approve_url(chain), headers=chain["superuser_headers"])
    assert r1.status_code == 201, r1.text
    r2 = client.post(_reject_url(chain), headers=chain["superuser_headers"])
    assert r2.status_code == 409, r2.text


def test_approve_after_reject_returns_409(client, db_session):
    chain = _build_chain(client, db_session)
    r1 = client.post(_reject_url(chain), headers=chain["superuser_headers"])
    assert r1.status_code == 201, r1.text
    r2 = client.post(_approve_url(chain), headers=chain["superuser_headers"])
    assert r2.status_code == 409, r2.text


def test_second_reject_returns_409(client, db_session):
    chain = _build_chain(client, db_session)
    r1 = client.post(_reject_url(chain), headers=chain["superuser_headers"])
    assert r1.status_code == 201, r1.text
    r2 = client.post(_reject_url(chain), headers=chain["superuser_headers"])
    assert r2.status_code == 409, r2.text


def test_409_detail_includes_existing_decision_value(client, db_session):
    chain = _build_chain(client, db_session)
    client.post(_approve_url(chain), headers=chain["superuser_headers"])
    r2 = client.post(_approve_url(chain), headers=chain["superuser_headers"])
    assert r2.status_code == 409, r2.text
    detail = r2.json()["detail"]
    assert "approved" in detail


def test_409_does_not_insert_second_row(client, db_session):
    """The DB must still contain exactly one decision after a rejected second attempt."""
    from sqlalchemy import select as sa_select

    from app.models.remediation_change_decision import RemediationChangeDecision

    chain = _build_chain(client, db_session)
    client.post(_approve_url(chain), headers=chain["superuser_headers"])
    client.post(_approve_url(chain), headers=chain["superuser_headers"])  # 409

    rows = (
        db_session.execute(
            sa_select(RemediationChangeDecision).where(
                RemediationChangeDecision.remediation_change_id == uuid.UUID(chain["change_id"])
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1
    assert rows[0].decision == "approved"


# ---------------------------------------------------------------------------
# Section I: GET /decision happy path
# ---------------------------------------------------------------------------


def test_get_decision_returns_existing_approved_decision(client, db_session):
    chain = _build_chain(client, db_session)
    post = client.post(
        _approve_url(chain),
        json={"comment": "Approved for real."},
        headers=chain["superuser_headers"],
    )
    assert post.status_code == 201, post.text
    created_id = post.json()["id"]

    get = client.get(_decision_url(chain), headers=chain["superuser_headers"])
    assert get.status_code == 200, get.text
    body = get.json()
    assert body["id"] == created_id
    assert body["decision"] == "approved"
    assert body["comment"] == "Approved for real."
    assert body["remediation_change_id"] == chain["change_id"]


def test_get_decision_returns_existing_rejected_decision(client, db_session):
    chain = _build_chain(client, db_session)
    client.post(_reject_url(chain), headers=chain["superuser_headers"])
    get = client.get(_decision_url(chain), headers=chain["superuser_headers"])
    assert get.status_code == 200, get.text
    assert get.json()["decision"] == "rejected"


def test_get_decision_response_includes_all_fields(client, db_session):
    """Every field in RemediationChangeDecisionRead must be present."""
    chain = _build_chain(client, db_session)
    client.post(_approve_url(chain), headers=chain["superuser_headers"])
    get = client.get(_decision_url(chain), headers=chain["superuser_headers"])
    assert get.status_code == 200, get.text
    body = get.json()
    required_fields = {
        "id", "organization_id", "remediation_run_id", "remediation_change_id",
        "decision", "reviewer_id", "reviewer_name", "reviewer_role",
        "decision_timestamp", "comment", "applied_at", "applied_by", "created_at",
    }
    for field in required_fields:
        assert field in body, f"Field '{field}' missing from GET /decision response"


# ---------------------------------------------------------------------------
# Section J: GET /decision — 404 when no decision exists yet
# ---------------------------------------------------------------------------


def test_get_decision_returns_404_when_no_decision_exists(client, db_session):
    chain = _build_chain(client, db_session)
    resp = client.get(_decision_url(chain), headers=chain["superuser_headers"])
    assert resp.status_code == 404, resp.text


def test_get_decision_unauthenticated_returns_401(client, db_session):
    chain = _build_chain(client, db_session)
    resp = client.get(_decision_url(chain))
    assert resp.status_code == 401, resp.text


# ---------------------------------------------------------------------------
# Section K: GET /decision accessible to non-superuser (org-wide read)
# ---------------------------------------------------------------------------


def test_regular_user_can_read_decision(client, db_session):
    """GET /decision is not gated on is_superuser -- any org member can read."""
    chain = _build_chain(client, db_session)
    client.post(_approve_url(chain), headers=chain["superuser_headers"])

    regular_headers = _register_nonsuperuser_in_org(client, db_session, chain["org_id"])
    resp = client.get(_decision_url(chain), headers=regular_headers)
    assert resp.status_code == 200, resp.text
    assert resp.json()["decision"] == "approved"


# ---------------------------------------------------------------------------
# Section L: Cross-org isolation
# ---------------------------------------------------------------------------


def test_other_org_superuser_cannot_approve_another_orgs_change(client, db_session):
    """A superuser from org B cannot approve a change that belongs to org A.
    The 4-layer 404 chain must return 404, not leak org A's data."""
    chain_a = _build_chain(client, db_session)
    chain_b = _build_chain(client, db_session)

    # org B superuser tries to approve org A's change using org A's URL
    resp = client.post(_approve_url(chain_a), headers=chain_b["superuser_headers"])
    # Must be 404 (not 403 or 200) — cross-tenant resources appear non-existent
    assert resp.status_code == 404, resp.text


def test_other_org_superuser_cannot_read_another_orgs_decision(client, db_session):
    chain_a = _build_chain(client, db_session)
    chain_b = _build_chain(client, db_session)

    client.post(_approve_url(chain_a), headers=chain_a["superuser_headers"])

    # org B tries to read org A's decision
    resp = client.get(_decision_url(chain_a), headers=chain_b["superuser_headers"])
    assert resp.status_code == 404, resp.text


def test_other_org_superuser_cannot_reject_another_orgs_change(client, db_session):
    chain_a = _build_chain(client, db_session)
    chain_b = _build_chain(client, db_session)
    resp = client.post(_reject_url(chain_a), headers=chain_b["superuser_headers"])
    assert resp.status_code == 404, resp.text
