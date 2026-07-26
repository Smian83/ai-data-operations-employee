"""Module 16 Phase 3 tests: bulk approve/reject endpoints and decision-status
enrichment on the Module 15 summary/change-list endpoints.

Endpoints under test
--------------------
  POST /{task_id}/runs/{run_id}/remediation/approve-all   → BulkDecisionResponse
  POST /{task_id}/runs/{run_id}/remediation/reject-all    → BulkDecisionResponse
  GET  /{task_id}/runs/{run_id}/remediation               → RemediationRunRead
       (decision_summary added in Phase 3)
  GET  /{task_id}/runs/{run_id}/remediation/changes       → list[RemediationChangeRead]
       (decision_status added in Phase 3)

Sections
--------
  A. approve-all: all changes pending → 200, all approved, skipped=0
  B. reject-all: all changes pending → 200, all rejected, skipped=0
  C. reviewer_name captured from full_name on bulk endpoints
  D. reviewer_role always "superuser" on bulk decisions
  E. applied_at / applied_by always NULL on newly created bulk decisions
  F. 403: non-superuser cannot call approve-all or reject-all
  G. 404: wrong task_id / run_id on bulk endpoints
  H. already decided (all skipped): second call → skipped_existing_count==total
  I. partially decided run: mixed pending + decided → only pending get touched
  J. mixed pending/approved: approve-all skips already-approved
  K. mixed pending/rejected: reject-all skips already-rejected
  L. cross-tenant isolation: cannot bulk-decide another org's run
  M. decision_summary on GET .../remediation
  N. decision_status on GET .../remediation/changes (single page)
  O. decision_status across multiple pages (pagination unchanged)
  P. idempotency: approve-all then approve-all → totals stable
  Q. large dataset: 500+ changes, bulk approve-all performance
  R. summary counts reflect bulk decisions correctly
  S. approve-all then reject-all: approved are skipped, new rejections counted
  T. unauthenticated calls → 401
"""
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from app.models.issue import Issue
from app.models.issue_detection_run import IssueDetectionRun
from app.models.remediation_change import RemediationChange
from app.models.remediation_change_decision import RemediationChangeDecision
from app.models.remediation_run import RemediationRun
from app.models.task import Task
from app.models.user import User as UserModel


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _register(client: TestClient, suffix: str):
    """Register an org + superuser, return (headers, user_row)."""
    resp = client.post(
        "/auth/register",
        json={
            "organization_name": f"Bulk Org {suffix}",
            "email": f"bulk-su-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Bulk Reviewer",
        },
    )
    assert resp.status_code == 201, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _login(client: TestClient, db_session, org_id, email: str, password: str = "correct-horse-battery"):
    """Look up org slug, then POST /auth/login."""
    from app.models.organization import Organization

    org = db_session.get(Organization, org_id)
    resp = client.post(
        "/auth/login",
        json={"organization_slug": org.slug, "email": email, "password": password},
    )
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _register_nonsuperuser(client: TestClient, db_session, org_id):
    """Insert a non-superuser into an existing org and return login headers."""
    suffix = uuid.uuid4().hex[:8]
    email = f"bulk-nosu-{suffix}@example.com"
    user = UserModel(
        organization_id=org_id,
        email=email,
        hashed_password=_hash_pw("correct-horse-battery"),
        full_name="Non Super",
        is_active=True,
        is_superuser=False,
    )
    db_session.add(user)
    db_session.commit()
    return _login(client, db_session, org_id, email)


def _hash_pw(raw: str) -> str:
    import bcrypt

    return bcrypt.hashpw(raw.encode(), bcrypt.gensalt()).decode()


def _build_chain(client: TestClient, db_session, suffix: str | None = None, n_changes: int = 1):
    """Build org → data-source → detect-task → detect-run (ORM) →
    remediate-task → remediate-task-run (via API) → RemediationRun (ORM) →
    n RemediationChange rows (ORM).

    Returns dict:
        su_headers, org_id, user_id, source_id,
        remediate_task_id, remediate_task_run_id (str),
        change_ids  (list[str], length == n_changes)
    """
    suffix = suffix or uuid.uuid4().hex[:8]
    su_headers = _register(client, suffix)

    user_row = db_session.execute(
        select(UserModel).where(UserModel.email == f"bulk-su-{suffix}@example.com")
    ).scalar_one()
    org_id = user_row.organization_id
    user_id = user_row.id

    # Data source
    src = client.post(
        "/data-sources",
        json={
            "name": f"Bulk DS {suffix}",
            "source_type": "csv_upload",
            "connection_metadata": {"file_path": "data.csv"},
        },
        headers=su_headers,
    )
    assert src.status_code == 201, src.text
    source_id = src.json()["id"]

    # DETECT task
    dt = client.post(
        "/tasks",
        json={"name": f"Bulk Detect {suffix}", "task_type": "detect", "data_source_id": source_id},
        headers=su_headers,
    )
    assert dt.status_code == 201, dt.text
    detect_task_id = dt.json()["id"]

    # DETECT TaskRun
    dr = client.post(f"/tasks/{detect_task_id}/runs", headers=su_headers)
    assert dr.status_code == 201, dr.text
    detect_run_id = uuid.UUID(dr.json()["id"])
    detect_task = db_session.get(Task, uuid.UUID(detect_task_id))

    # IssueDetectionRun
    idr = IssueDetectionRun(
        organization_id=org_id,
        task_run_id=detect_run_id,
        task_id=detect_task.id,
        data_source_id=uuid.UUID(source_id),
        rows_scanned=n_changes,
        columns_scanned=1,
        total_issues_found=n_changes,
        persisted_issue_count=n_changes,
        issues_by_severity={"LOW": n_changes},
        issues_by_type={"leading_whitespace": n_changes},
        limits_applied={"max_persisted_issues": 10_000},
        detection_engine_version="1.0",
        source_sha256="b" * 64,
    )
    db_session.add(idr)
    db_session.commit()
    db_session.refresh(idr)

    # Issues
    issues = []
    for i in range(n_changes):
        issue = Issue(
            organization_id=org_id,
            detection_run_id=idr.id,
            row_number=i + 1,
            column_name="name",
            issue_type="leading_whitespace",
            severity="LOW",
            original_value=f"  Alice{i}",
            suggested_fix=f"Alice{i}",
            confidence=1.0,
        )
        db_session.add(issue)
    db_session.commit()
    db_session.refresh(idr)  # re-attach
    issues = db_session.execute(
        select(Issue).where(Issue.detection_run_id == idr.id).order_by(Issue.row_number)
    ).scalars().all()

    # REMEDIATE task
    rt = client.post(
        "/tasks",
        json={
            "name": f"Bulk Remediate {suffix}",
            "task_type": "remediate",
            "data_source_id": source_id,
        },
        headers=su_headers,
    )
    assert rt.status_code == 201, rt.text
    remediate_task_id = rt.json()["id"]

    # REMEDIATE TaskRun
    rr_resp = client.post(
        f"/tasks/{remediate_task_id}/runs",
        json={"source_task_run_id": str(detect_run_id)},
        headers=su_headers,
    )
    assert rr_resp.status_code == 201, rr_resp.text
    remediate_task_run_id = uuid.UUID(rr_resp.json()["id"])  # TaskRun.id == URL run_id

    remediate_task = db_session.get(Task, uuid.UUID(remediate_task_id))

    # RemediationRun (ORM)
    rrun = RemediationRun(
        organization_id=org_id,
        task_run_id=remediate_task_run_id,
        task_id=remediate_task.id,
        data_source_id=uuid.UUID(source_id),
        source_task_run_id=detect_run_id,
        issues_considered_count=n_changes,
        total_changes_count=n_changes,
        issues_skipped_count=0,
        changes_by_action={"trim_whitespace": n_changes},
        skipped_by_reason={},
        remediation_engine_version="1.0",
    )
    db_session.add(rrun)
    db_session.commit()
    db_session.refresh(rrun)

    # RemediationChange rows
    change_ids = []
    for i, issue in enumerate(issues):
        rc = RemediationChange(
            organization_id=org_id,
            remediation_run_id=rrun.id,
            source_issue_id=issue.id,
            row_number=i + 1,
            column_name="name",
            action="trim_whitespace",
            original_value=f"  Alice{i}",
            proposed_value=f"Alice{i}",
            reason="leading whitespace removed",
            confidence=1.0,
        )
        db_session.add(rc)
    db_session.commit()

    # Reload change IDs in row_number order
    rows = db_session.execute(
        select(RemediationChange)
        .where(RemediationChange.remediation_run_id == rrun.id)
        .order_by(RemediationChange.row_number)
    ).scalars().all()
    change_ids = [str(r.id) for r in rows]

    return {
        "su_headers": su_headers,
        "org_id": org_id,
        "user_id": user_id,
        "source_id": source_id,
        "remediate_task_id": remediate_task_id,
        "remediate_task_run_id": str(remediate_task_run_id),
        "change_ids": change_ids,
    }


def _approve_all(client, chain, *, expected=200, **kw):
    tid = chain["remediate_task_id"]
    rid = chain["remediate_task_run_id"]
    return client.post(
        f"/tasks/{tid}/runs/{rid}/remediation/approve-all",
        headers=chain["su_headers"],
        **kw,
    )


def _reject_all(client, chain, *, expected=200, **kw):
    tid = chain["remediate_task_id"]
    rid = chain["remediate_task_run_id"]
    return client.post(
        f"/tasks/{tid}/runs/{rid}/remediation/reject-all",
        headers=chain["su_headers"],
        **kw,
    )


# ---------------------------------------------------------------------------
# Section A: approve-all happy path
# ---------------------------------------------------------------------------


def test_approve_all_all_pending(client, db_session):
    """All 3 changes pending → approved_count=3, skipped=0."""
    chain = _build_chain(client, db_session, suffix="aall", n_changes=3)
    resp = _approve_all(client, chain)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_changes"] == 3
    assert body["approved_count"] == 3
    assert body["rejected_count"] == 0
    assert body["skipped_existing_count"] == 0


def test_approve_all_single_change(client, db_session):
    chain = _build_chain(client, db_session, suffix="aone")
    resp = _approve_all(client, chain)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_changes"] == 1
    assert body["approved_count"] == 1
    assert body["skipped_existing_count"] == 0


def test_approve_all_zero_changes(client, db_session):
    """An empty run (no changes) → all zeros."""
    chain = _build_chain(client, db_session, suffix="azero", n_changes=0)
    resp = _approve_all(client, chain)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_changes"] == 0
    assert body["approved_count"] == 0
    assert body["skipped_existing_count"] == 0


# ---------------------------------------------------------------------------
# Section B: reject-all happy path
# ---------------------------------------------------------------------------


def test_reject_all_all_pending(client, db_session):
    chain = _build_chain(client, db_session, suffix="rall", n_changes=3)
    resp = _reject_all(client, chain)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_changes"] == 3
    assert body["rejected_count"] == 3
    assert body["approved_count"] == 0
    assert body["skipped_existing_count"] == 0


def test_reject_all_single_change(client, db_session):
    chain = _build_chain(client, db_session, suffix="rone")
    resp = _reject_all(client, chain)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["total_changes"] == 1
    assert body["rejected_count"] == 1
    assert body["approved_count"] == 0
    assert body["skipped_existing_count"] == 0


# ---------------------------------------------------------------------------
# Section C: reviewer_name captured from full_name
# ---------------------------------------------------------------------------


def test_approve_all_reviewer_name_from_full_name(client, db_session):
    """Bulk-approve should record reviewer_name = superuser's full_name."""
    chain = _build_chain(client, db_session, suffix="crn")
    resp = _approve_all(client, chain)
    assert resp.status_code == 200, resp.text

    # Verify DB records
    rcd_rows = db_session.execute(
        select(RemediationChangeDecision).where(
            RemediationChangeDecision.organization_id == chain["org_id"]
        )
    ).scalars().all()
    assert len(rcd_rows) == 1
    assert rcd_rows[0].reviewer_name == "Bulk Reviewer"


# ---------------------------------------------------------------------------
# Section D: reviewer_role always "superuser"
# ---------------------------------------------------------------------------


def test_approve_all_reviewer_role_is_superuser(client, db_session):
    chain = _build_chain(client, db_session, suffix="drl", n_changes=2)
    _approve_all(client, chain)
    rows = db_session.execute(
        select(RemediationChangeDecision).where(
            RemediationChangeDecision.organization_id == chain["org_id"]
        )
    ).scalars().all()
    assert all(r.reviewer_role == "superuser" for r in rows)


# ---------------------------------------------------------------------------
# Section E: applied_at / applied_by always NULL
# ---------------------------------------------------------------------------


def test_approve_all_applied_fields_are_null(client, db_session):
    chain = _build_chain(client, db_session, suffix="enull")
    _approve_all(client, chain)
    rows = db_session.execute(
        select(RemediationChangeDecision).where(
            RemediationChangeDecision.organization_id == chain["org_id"]
        )
    ).scalars().all()
    for row in rows:
        assert row.applied_at is None
        assert row.applied_by is None


# ---------------------------------------------------------------------------
# Section F: non-superuser gets 403
# ---------------------------------------------------------------------------


def test_approve_all_nonsuperuser_403(client, db_session):
    chain = _build_chain(client, db_session, suffix="fapprv")
    ns_headers = _register_nonsuperuser(client, db_session, chain["org_id"])
    tid = chain["remediate_task_id"]
    rid = chain["remediate_task_run_id"]
    resp = client.post(f"/tasks/{tid}/runs/{rid}/remediation/approve-all", headers=ns_headers)
    assert resp.status_code == 403


def test_reject_all_nonsuperuser_403(client, db_session):
    chain = _build_chain(client, db_session, suffix="frej")
    ns_headers = _register_nonsuperuser(client, db_session, chain["org_id"])
    tid = chain["remediate_task_id"]
    rid = chain["remediate_task_run_id"]
    resp = client.post(f"/tasks/{tid}/runs/{rid}/remediation/reject-all", headers=ns_headers)
    assert resp.status_code == 403


# ---------------------------------------------------------------------------
# Section G: 404 for wrong task_id / run_id
# ---------------------------------------------------------------------------


def test_approve_all_wrong_task_id_404(client, db_session):
    chain = _build_chain(client, db_session, suffix="g404t")
    rid = chain["remediate_task_run_id"]
    resp = client.post(
        f"/tasks/{uuid.uuid4()}/runs/{rid}/remediation/approve-all",
        headers=chain["su_headers"],
    )
    assert resp.status_code == 404


def test_approve_all_wrong_run_id_404(client, db_session):
    chain = _build_chain(client, db_session, suffix="g404r")
    tid = chain["remediate_task_id"]
    resp = client.post(
        f"/tasks/{tid}/runs/{uuid.uuid4()}/remediation/approve-all",
        headers=chain["su_headers"],
    )
    assert resp.status_code == 404


def test_reject_all_wrong_task_id_404(client, db_session):
    chain = _build_chain(client, db_session, suffix="g404tr")
    rid = chain["remediate_task_run_id"]
    resp = client.post(
        f"/tasks/{uuid.uuid4()}/runs/{rid}/remediation/reject-all",
        headers=chain["su_headers"],
    )
    assert resp.status_code == 404


def test_reject_all_wrong_run_id_404(client, db_session):
    chain = _build_chain(client, db_session, suffix="g404rr")
    tid = chain["remediate_task_id"]
    resp = client.post(
        f"/tasks/{tid}/runs/{uuid.uuid4()}/remediation/reject-all",
        headers=chain["su_headers"],
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Section H: all changes already decided → all skipped
# ---------------------------------------------------------------------------


def test_approve_all_already_all_decided_skips_all(client, db_session):
    """approve-all on a run where every change is already approved: skipped=total."""
    chain = _build_chain(client, db_session, suffix="hallskip", n_changes=3)
    # First call decides everything
    r1 = _approve_all(client, chain)
    assert r1.status_code == 200
    # Second call: nothing new
    r2 = _approve_all(client, chain)
    assert r2.status_code == 200
    body = r2.json()
    assert body["total_changes"] == 3
    assert body["approved_count"] == 0
    assert body["rejected_count"] == 0
    assert body["skipped_existing_count"] == 3


def test_reject_all_already_all_decided_skips_all(client, db_session):
    chain = _build_chain(client, db_session, suffix="hrallskip", n_changes=2)
    r1 = _reject_all(client, chain)
    assert r1.status_code == 200
    r2 = _reject_all(client, chain)
    assert r2.status_code == 200
    body = r2.json()
    assert body["skipped_existing_count"] == 2
    assert body["rejected_count"] == 0


# ---------------------------------------------------------------------------
# Section I: partially decided run
# ---------------------------------------------------------------------------


def test_approve_all_partially_decided_only_pending_touched(client, db_session):
    """3 changes: 1 already approved → approve-all touches only the 2 pending."""
    chain = _build_chain(client, db_session, suffix="ipart", n_changes=3)
    tid = chain["remediate_task_id"]
    rid = chain["remediate_task_run_id"]
    first_change_id = chain["change_ids"][0]

    # Individually approve the first change
    r = client.post(
        f"/tasks/{tid}/runs/{rid}/remediation/changes/{first_change_id}/approve",
        headers=chain["su_headers"],
    )
    assert r.status_code == 201, r.text

    # Now bulk-approve remaining
    resp = _approve_all(client, chain)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_changes"] == 3
    assert body["approved_count"] == 2  # only the 2 pending
    assert body["skipped_existing_count"] == 1  # first one was pre-decided


def test_reject_all_partially_decided_only_pending_touched(client, db_session):
    """3 changes: 1 already rejected → reject-all touches only the 2 pending."""
    chain = _build_chain(client, db_session, suffix="ipartr", n_changes=3)
    tid = chain["remediate_task_id"]
    rid = chain["remediate_task_run_id"]
    first_change_id = chain["change_ids"][0]

    r = client.post(
        f"/tasks/{tid}/runs/{rid}/remediation/changes/{first_change_id}/reject",
        headers=chain["su_headers"],
    )
    assert r.status_code == 201, r.text

    resp = _reject_all(client, chain)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_changes"] == 3
    assert body["rejected_count"] == 2
    assert body["skipped_existing_count"] == 1


# ---------------------------------------------------------------------------
# Section J: mixed pending/approved → approve-all skips already-approved
# ---------------------------------------------------------------------------


def test_approve_all_mixed_pending_approved(client, db_session):
    """2 approved + 3 pending: approve-all adds 3, skips 2."""
    chain = _build_chain(client, db_session, suffix="jmpa", n_changes=5)
    tid = chain["remediate_task_id"]
    rid = chain["remediate_task_run_id"]

    # Individually approve changes 0 and 1
    for cid in chain["change_ids"][:2]:
        r = client.post(
            f"/tasks/{tid}/runs/{rid}/remediation/changes/{cid}/approve",
            headers=chain["su_headers"],
        )
        assert r.status_code == 201

    resp = _approve_all(client, chain)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_changes"] == 5
    assert body["approved_count"] == 3
    assert body["skipped_existing_count"] == 2


# ---------------------------------------------------------------------------
# Section K: mixed pending/rejected → reject-all skips already-rejected
# ---------------------------------------------------------------------------


def test_reject_all_mixed_pending_rejected(client, db_session):
    """2 rejected + 3 pending: reject-all adds 3, skips 2."""
    chain = _build_chain(client, db_session, suffix="kmpr", n_changes=5)
    tid = chain["remediate_task_id"]
    rid = chain["remediate_task_run_id"]

    for cid in chain["change_ids"][:2]:
        r = client.post(
            f"/tasks/{tid}/runs/{rid}/remediation/changes/{cid}/reject",
            headers=chain["su_headers"],
        )
        assert r.status_code == 201

    resp = _reject_all(client, chain)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_changes"] == 5
    assert body["rejected_count"] == 3
    assert body["skipped_existing_count"] == 2


# ---------------------------------------------------------------------------
# Section L: cross-tenant isolation
# ---------------------------------------------------------------------------


def test_approve_all_cross_org_returns_404(client, db_session):
    """Org-B's superuser cannot call approve-all on Org-A's run."""
    chain_a = _build_chain(client, db_session, suffix="lxa")
    chain_b = _build_chain(client, db_session, suffix="lxb")

    tid_a = chain_a["remediate_task_id"]
    rid_a = chain_a["remediate_task_run_id"]

    resp = client.post(
        f"/tasks/{tid_a}/runs/{rid_a}/remediation/approve-all",
        headers=chain_b["su_headers"],  # Org-B's token against Org-A's resource
    )
    assert resp.status_code == 404


def test_reject_all_cross_org_returns_404(client, db_session):
    chain_a = _build_chain(client, db_session, suffix="lxra")
    chain_b = _build_chain(client, db_session, suffix="lxrb")

    tid_a = chain_a["remediate_task_id"]
    rid_a = chain_a["remediate_task_run_id"]

    resp = client.post(
        f"/tasks/{tid_a}/runs/{rid_a}/remediation/reject-all",
        headers=chain_b["su_headers"],
    )
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Section M: decision_summary on GET .../remediation
# ---------------------------------------------------------------------------


def test_decision_summary_all_pending(client, db_session):
    """Before any decisions, summary should be: pending=N, approved=0, rejected=0."""
    chain = _build_chain(client, db_session, suffix="msumpend", n_changes=4)
    tid = chain["remediate_task_id"]
    rid = chain["remediate_task_run_id"]

    resp = client.get(f"/tasks/{tid}/runs/{rid}/remediation", headers=chain["su_headers"])
    assert resp.status_code == 200, resp.text
    body = resp.json()
    ds = body["decision_summary"]
    assert ds["pending"] == 4
    assert ds["approved"] == 0
    assert ds["rejected"] == 0


def test_decision_summary_after_approve_all(client, db_session):
    chain = _build_chain(client, db_session, suffix="msumappr", n_changes=3)
    _approve_all(client, chain)

    tid = chain["remediate_task_id"]
    rid = chain["remediate_task_run_id"]
    resp = client.get(f"/tasks/{tid}/runs/{rid}/remediation", headers=chain["su_headers"])
    assert resp.status_code == 200
    ds = resp.json()["decision_summary"]
    assert ds["pending"] == 0
    assert ds["approved"] == 3
    assert ds["rejected"] == 0


def test_decision_summary_after_reject_all(client, db_session):
    chain = _build_chain(client, db_session, suffix="msumrej", n_changes=3)
    _reject_all(client, chain)

    tid = chain["remediate_task_id"]
    rid = chain["remediate_task_run_id"]
    resp = client.get(f"/tasks/{tid}/runs/{rid}/remediation", headers=chain["su_headers"])
    assert resp.status_code == 200
    ds = resp.json()["decision_summary"]
    assert ds["pending"] == 0
    assert ds["approved"] == 0
    assert ds["rejected"] == 3


def test_decision_summary_mixed(client, db_session):
    """1 approved individually, 1 rejected individually, 1 pending."""
    chain = _build_chain(client, db_session, suffix="msummix", n_changes=3)
    tid = chain["remediate_task_id"]
    rid = chain["remediate_task_run_id"]
    cids = chain["change_ids"]

    client.post(
        f"/tasks/{tid}/runs/{rid}/remediation/changes/{cids[0]}/approve",
        headers=chain["su_headers"],
    )
    client.post(
        f"/tasks/{tid}/runs/{rid}/remediation/changes/{cids[1]}/reject",
        headers=chain["su_headers"],
    )

    resp = client.get(f"/tasks/{tid}/runs/{rid}/remediation", headers=chain["su_headers"])
    ds = resp.json()["decision_summary"]
    assert ds["pending"] == 1
    assert ds["approved"] == 1
    assert ds["rejected"] == 1


def test_decision_summary_keys_always_present(client, db_session):
    """Even with zero decisions, the three keys must exist."""
    chain = _build_chain(client, db_session, suffix="msumkeys", n_changes=2)
    tid = chain["remediate_task_id"]
    rid = chain["remediate_task_run_id"]
    resp = client.get(f"/tasks/{tid}/runs/{rid}/remediation", headers=chain["su_headers"])
    ds = resp.json()["decision_summary"]
    assert set(ds.keys()) == {"pending", "approved", "rejected"}


# ---------------------------------------------------------------------------
# Section N: decision_status on GET .../remediation/changes (single page)
# ---------------------------------------------------------------------------


def test_decision_status_default_pending(client, db_session):
    """No decisions yet → every change in the list has decision_status='pending'."""
    chain = _build_chain(client, db_session, suffix="npend", n_changes=3)
    tid = chain["remediate_task_id"]
    rid = chain["remediate_task_run_id"]

    resp = client.get(f"/tasks/{tid}/runs/{rid}/remediation/changes", headers=chain["su_headers"])
    assert resp.status_code == 200
    items = resp.json()["items"]
    assert len(items) == 3
    assert all(it["decision_status"] == "pending" for it in items)


def test_decision_status_after_approve_all(client, db_session):
    chain = _build_chain(client, db_session, suffix="nstatappr", n_changes=3)
    _approve_all(client, chain)

    tid = chain["remediate_task_id"]
    rid = chain["remediate_task_run_id"]
    resp = client.get(f"/tasks/{tid}/runs/{rid}/remediation/changes", headers=chain["su_headers"])
    items = resp.json()["items"]
    assert all(it["decision_status"] == "approved" for it in items)


def test_decision_status_after_reject_all(client, db_session):
    chain = _build_chain(client, db_session, suffix="nstatrej", n_changes=3)
    _reject_all(client, chain)

    tid = chain["remediate_task_id"]
    rid = chain["remediate_task_run_id"]
    resp = client.get(f"/tasks/{tid}/runs/{rid}/remediation/changes", headers=chain["su_headers"])
    items = resp.json()["items"]
    assert all(it["decision_status"] == "rejected" for it in items)


def test_decision_status_mixed_per_change(client, db_session):
    """Individual decisions per change show up correctly in the list."""
    chain = _build_chain(client, db_session, suffix="nstatmix", n_changes=3)
    tid = chain["remediate_task_id"]
    rid = chain["remediate_task_run_id"]
    cids = chain["change_ids"]

    client.post(
        f"/tasks/{tid}/runs/{rid}/remediation/changes/{cids[0]}/approve",
        headers=chain["su_headers"],
    )
    client.post(
        f"/tasks/{tid}/runs/{rid}/remediation/changes/{cids[1]}/reject",
        headers=chain["su_headers"],
    )
    # cids[2] remains pending

    resp = client.get(f"/tasks/{tid}/runs/{rid}/remediation/changes", headers=chain["su_headers"])
    items = resp.json()["items"]
    status_by_id = {it["id"]: it["decision_status"] for it in items}
    assert status_by_id[cids[0]] == "approved"
    assert status_by_id[cids[1]] == "rejected"
    assert status_by_id[cids[2]] == "pending"


# ---------------------------------------------------------------------------
# Section O: decision_status across multiple pages (pagination unchanged)
# ---------------------------------------------------------------------------


def test_decision_status_across_pages(client, db_session):
    """5 changes: approve-all; paginate limit=2 and verify all approved."""
    chain = _build_chain(client, db_session, suffix="opagstat", n_changes=5)
    _approve_all(client, chain)

    tid = chain["remediate_task_id"]
    rid = chain["remediate_task_run_id"]

    statuses = []
    offset = 0
    limit = 2
    while True:
        resp = client.get(
            f"/tasks/{tid}/runs/{rid}/remediation/changes",
            params={"limit": limit, "offset": offset},
            headers=chain["su_headers"],
        )
        assert resp.status_code == 200
        body = resp.json()
        page_items = body["items"]
        statuses.extend(it["decision_status"] for it in page_items)
        offset += limit
        if offset >= body["total"]:
            break

    assert len(statuses) == 5
    assert all(s == "approved" for s in statuses)


def test_pagination_total_unchanged_after_bulk(client, db_session):
    """approve-all must not change total or limit."""
    chain = _build_chain(client, db_session, suffix="opagcount", n_changes=6)
    tid = chain["remediate_task_id"]
    rid = chain["remediate_task_run_id"]

    before = client.get(
        f"/tasks/{tid}/runs/{rid}/remediation/changes",
        params={"limit": 3},
        headers=chain["su_headers"],
    ).json()

    _approve_all(client, chain)

    after = client.get(
        f"/tasks/{tid}/runs/{rid}/remediation/changes",
        params={"limit": 3},
        headers=chain["su_headers"],
    ).json()

    assert before["total"] == after["total"]
    assert before["limit"] == after["limit"]


# ---------------------------------------------------------------------------
# Section P: idempotency
# ---------------------------------------------------------------------------


def test_approve_all_idempotent_second_call(client, db_session):
    """approve-all called twice: second call has approved_count=0, skipped=total."""
    chain = _build_chain(client, db_session, suffix="pidem", n_changes=4)
    r1 = _approve_all(client, chain)
    assert r1.status_code == 200
    b1 = r1.json()

    r2 = _approve_all(client, chain)
    assert r2.status_code == 200
    b2 = r2.json()

    # Nothing new was written
    assert b2["approved_count"] == 0
    assert b2["skipped_existing_count"] == b1["total_changes"]

    # Total_changes stable
    assert b1["total_changes"] == b2["total_changes"]


def test_reject_all_idempotent_second_call(client, db_session):
    chain = _build_chain(client, db_session, suffix="pidemr", n_changes=4)
    r1 = _reject_all(client, chain)
    r2 = _reject_all(client, chain)
    assert r2.json()["rejected_count"] == 0
    assert r2.json()["skipped_existing_count"] == r1.json()["total_changes"]


# ---------------------------------------------------------------------------
# Section Q: large dataset (500+ changes)
# ---------------------------------------------------------------------------


def test_approve_all_large_dataset(client, db_session):
    """Bulk-approve 500 pending changes. Must complete and return correct counts."""
    chain = _build_chain(client, db_session, suffix="qlarge", n_changes=500)
    resp = _approve_all(client, chain)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_changes"] == 500
    assert body["approved_count"] == 500
    assert body["skipped_existing_count"] == 0


def test_reject_all_large_dataset(client, db_session):
    chain = _build_chain(client, db_session, suffix="qlarger", n_changes=500)
    resp = _reject_all(client, chain)
    assert resp.status_code == 200
    body = resp.json()
    assert body["total_changes"] == 500
    assert body["rejected_count"] == 500
    assert body["skipped_existing_count"] == 0


def test_large_dataset_second_bulk_call_skips_all(client, db_session):
    """After bulk-approving 500, a second approve-all skips all 500."""
    chain = _build_chain(client, db_session, suffix="qlargedem", n_changes=500)
    _approve_all(client, chain)
    r2 = _approve_all(client, chain)
    body = r2.json()
    assert body["approved_count"] == 0
    assert body["skipped_existing_count"] == 500


# ---------------------------------------------------------------------------
# Section R: summary counts match bulk decisions
# ---------------------------------------------------------------------------


def test_summary_counts_after_partial_then_bulk(client, db_session):
    """2 individually rejected, then approve-all → summary shows approved=3, rejected=2."""
    chain = _build_chain(client, db_session, suffix="rsumctrl", n_changes=5)
    tid = chain["remediate_task_id"]
    rid = chain["remediate_task_run_id"]
    cids = chain["change_ids"]

    for cid in cids[:2]:
        client.post(
            f"/tasks/{tid}/runs/{rid}/remediation/changes/{cid}/reject",
            headers=chain["su_headers"],
        )

    _approve_all(client, chain)

    resp = client.get(f"/tasks/{tid}/runs/{rid}/remediation", headers=chain["su_headers"])
    ds = resp.json()["decision_summary"]
    assert ds["pending"] == 0
    assert ds["approved"] == 3   # the 3 that were pending
    assert ds["rejected"] == 2   # the 2 individually rejected


def test_summary_pending_decrements_as_decisions_added(client, db_session):
    """Verify pending decrements step-by-step as individual decisions are made."""
    chain = _build_chain(client, db_session, suffix="rsumdec", n_changes=3)
    tid = chain["remediate_task_id"]
    rid = chain["remediate_task_run_id"]
    cids = chain["change_ids"]

    def _summary():
        return client.get(
            f"/tasks/{tid}/runs/{rid}/remediation", headers=chain["su_headers"]
        ).json()["decision_summary"]

    assert _summary()["pending"] == 3

    client.post(
        f"/tasks/{tid}/runs/{rid}/remediation/changes/{cids[0]}/approve",
        headers=chain["su_headers"],
    )
    assert _summary()["pending"] == 2

    client.post(
        f"/tasks/{tid}/runs/{rid}/remediation/changes/{cids[1]}/reject",
        headers=chain["su_headers"],
    )
    assert _summary()["pending"] == 1
    assert _summary()["approved"] == 1
    assert _summary()["rejected"] == 1


# ---------------------------------------------------------------------------
# Section S: approve-all then reject-all (approved are skipped)
# ---------------------------------------------------------------------------


def test_approve_then_reject_all_approved_skipped(client, db_session):
    """After approve-all, reject-all sees all changes as already decided → skipped."""
    chain = _build_chain(client, db_session, suffix="saprthenrej", n_changes=4)
    ra = _approve_all(client, chain)
    assert ra.json()["approved_count"] == 4

    rr = _reject_all(client, chain)
    assert rr.status_code == 200
    body = rr.json()
    assert body["rejected_count"] == 0
    assert body["skipped_existing_count"] == 4


def test_reject_then_approve_all_rejected_skipped(client, db_session):
    chain = _build_chain(client, db_session, suffix="srejthenapp", n_changes=4)
    _reject_all(client, chain)
    ra = _approve_all(client, chain)
    assert ra.json()["approved_count"] == 0
    assert ra.json()["skipped_existing_count"] == 4


def test_partial_approve_then_reject_all(client, db_session):
    """2 individually approved, then reject-all: 3 rejected, 2 skipped."""
    chain = _build_chain(client, db_session, suffix="spartthenrej", n_changes=5)
    tid = chain["remediate_task_id"]
    rid = chain["remediate_task_run_id"]

    for cid in chain["change_ids"][:2]:
        client.post(
            f"/tasks/{tid}/runs/{rid}/remediation/changes/{cid}/approve",
            headers=chain["su_headers"],
        )

    resp = _reject_all(client, chain)
    assert resp.status_code == 200
    body = resp.json()
    assert body["rejected_count"] == 3
    assert body["skipped_existing_count"] == 2


# ---------------------------------------------------------------------------
# Section T: unauthenticated → 401
# ---------------------------------------------------------------------------


def test_approve_all_no_auth_401(client, db_session):
    chain = _build_chain(client, db_session, suffix="tnoauth")
    tid = chain["remediate_task_id"]
    rid = chain["remediate_task_run_id"]
    resp = client.post(f"/tasks/{tid}/runs/{rid}/remediation/approve-all")
    assert resp.status_code == 401


def test_reject_all_no_auth_401(client, db_session):
    chain = _build_chain(client, db_session, suffix="tnoauthr")
    tid = chain["remediate_task_id"]
    rid = chain["remediate_task_run_id"]
    resp = client.post(f"/tasks/{tid}/runs/{rid}/remediation/reject-all")
    assert resp.status_code == 401
