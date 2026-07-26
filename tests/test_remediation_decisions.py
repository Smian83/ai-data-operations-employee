"""Module 16 Phase 1 tests: RemediationChangeDecision model, ORM
mapping, enum/constraint consistency, and append-only semantics.

No API endpoints exist yet (Phase 2). All tests work directly against
the ORM and database layer. Tests are designed to pass on both SQLite
(sandbox) and PostgreSQL (real-Postgres verification).

Sections:
  A. Constant and enum consistency
  B. Model construction and persistence (basic round-trip)
  C. Append-only semantics (multiple rows per change allowed)
  D. Nullable field behavior (reviewer_name/role/comment, applied fields)
  E. Constraint enforcement (CHECK, NOT NULL, FK presence)
  F. ORM vs PRAGMA / INFORMATION_SCHEMA schema audit
"""
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import inspect, select, text

from app.models.enums import REMEDIATION_CHANGE_DECISION_VALUES
from app.models.issue import Issue
from app.models.issue_detection_run import IssueDetectionRun
from app.models.remediation_change import RemediationChange
from app.models.remediation_run import RemediationRun
from app.models.task import Task
from app.models.remediation_change_decision import RemediationChangeDecision


# ---------------------------------------------------------------------------
# Section A: Constant and enum consistency
# ---------------------------------------------------------------------------


def test_decision_values_tuple_has_exactly_two_entries():
    """The closed vocabulary is exactly {approved, rejected}."""
    assert len(REMEDIATION_CHANGE_DECISION_VALUES) == 2


def test_decision_values_are_approved_and_rejected():
    assert "approved" in REMEDIATION_CHANGE_DECISION_VALUES
    assert "rejected" in REMEDIATION_CHANGE_DECISION_VALUES


def test_decision_values_are_unique():
    assert len(set(REMEDIATION_CHANGE_DECISION_VALUES)) == len(REMEDIATION_CHANGE_DECISION_VALUES)


def test_decision_values_match_check_constraint_expression():
    """The CHECK constraint name exists on the model's __table_args__
    and contains exactly the values from REMEDIATION_CHANGE_DECISION_VALUES."""
    from sqlalchemy import CheckConstraint

    check_constraint = None
    for arg in RemediationChangeDecision.__table_args__:
        if isinstance(arg, CheckConstraint):
            if "decision" in str(arg.sqltext):
                check_constraint = arg
                break
    assert check_constraint is not None, "CHECK constraint on 'decision' not found"
    constraint_text = str(check_constraint.sqltext)
    for value in REMEDIATION_CHANGE_DECISION_VALUES:
        assert f"'{value}'" in constraint_text, (
            f"Value '{value}' not found in CHECK constraint: {constraint_text}"
        )


def test_decision_values_all_short_enough_for_varchar_10():
    """VARCHAR(10) column -- all decision values must fit."""
    for value in REMEDIATION_CHANGE_DECISION_VALUES:
        assert len(value) <= 10, f"Decision value '{value}' exceeds VARCHAR(10)"


def test_identifier_names_within_postgres_namedatalen():
    """All named constraints and indexes must be ≤ 63 bytes (PostgreSQL's
    NAMEDATALEN limit). Pre-measured in the design doc; this test catches
    any future rename that would exceed the limit."""
    from sqlalchemy import CheckConstraint, ForeignKey, ForeignKeyConstraint, UniqueConstraint

    names = []
    for arg in RemediationChangeDecision.__table_args__:
        if isinstance(arg, (CheckConstraint, UniqueConstraint, ForeignKeyConstraint)):
            if hasattr(arg, "name") and arg.name:
                names.append(arg.name)
        elif hasattr(arg, "name") and arg.name:  # Index
            names.append(arg.name)

    # Also collect inline FK names from mapped columns
    for col in RemediationChangeDecision.__table__.columns:
        for fk in col.foreign_keys:
            if fk.name:
                names.append(fk.name)
            elif hasattr(fk.constraint, "name") and fk.constraint.name:
                names.append(fk.constraint.name)

    assert names, "No named constraints/indexes found on RemediationChangeDecision"
    for name in names:
        assert len(name.encode()) <= 63, (
            f"Identifier '{name}' is {len(name.encode())} bytes, exceeds "
            f"PostgreSQL's 63-byte NAMEDATALEN limit"
        )


# ---------------------------------------------------------------------------
# Section B: Helper — builds the prerequisite chain via the HTTP API
# (same pattern as test_remediation_api.py's _build_run_with_one_change)
# ---------------------------------------------------------------------------


def _make_run_with_change(client: TestClient, db_session):
    """Register org+user, build a DETECT run, then insert RemediationRun
    + RemediationChange via direct ORM (same approach as test_remediation_api.py).
    Returns (org_id, user_id, remediation_run_id, remediation_change_id)."""
    suffix = uuid.uuid4().hex[:8]
    resp = client.post(
        "/auth/register",
        json={
            "organization_name": f"Decision Org {suffix}",
            "email": f"dec-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Decision Reviewer",
        },
    )
    assert resp.status_code == 201, resp.text
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    # Fetch the created user from DB (register response only returns the token)
    from app.models.user import User as UserModel
    from sqlalchemy import select as sa_select
    user_row = db_session.execute(
        sa_select(UserModel).where(UserModel.email == f"dec-{suffix}@example.com")
    ).scalar_one()
    user_id = user_row.id

    # Data source
    src_resp = client.post(
        "/data-sources",
        json={
            "name": f"DS {suffix}",
            "source_type": "csv_upload",
            "connection_metadata": {"file_path": "test.csv"},
        },
        headers=headers,
    )
    assert src_resp.status_code == 201, src_resp.text
    source_id = src_resp.json()["id"]
    org_id = uuid.UUID(src_resp.json()["organization_id"])

    # DETECT task + run
    dt_resp = client.post(
        "/tasks",
        json={"name": "Detect", "task_type": "detect", "data_source_id": source_id},
        headers=headers,
    )
    assert dt_resp.status_code == 201, dt_resp.text
    detect_task_id = dt_resp.json()["id"]

    dr_resp = client.post(f"/tasks/{detect_task_id}/runs", headers=headers)
    assert dr_resp.status_code == 201, dr_resp.text
    detect_run_id = uuid.UUID(dr_resp.json()["id"])

    detect_task = db_session.get(Task, uuid.UUID(detect_task_id))

    # IssueDetectionRun (direct ORM insert)
    idr = IssueDetectionRun(
        organization_id=org_id,
        task_run_id=detect_run_id,
        task_id=detect_task.id,
        data_source_id=uuid.UUID(source_id),
        rows_scanned=1,
        columns_scanned=1,
        total_issues_found=1,
        persisted_issue_count=1,
        issues_by_severity={"MEDIUM": 1},
        issues_by_type={"invalid_email": 1},
        limits_applied={"max_persisted_issues": 10_000},
        detection_engine_version="1.0",
        source_sha256="a" * 64,
    )
    db_session.add(idr)
    db_session.commit()
    db_session.refresh(idr)

    issue = Issue(
        organization_id=org_id,
        detection_run_id=idr.id,
        row_number=0,
        column_name="email",
        issue_type="invalid_email",
        severity="MEDIUM",
        original_value="bad",
        confidence=1.0,
    )
    db_session.add(issue)
    db_session.commit()
    db_session.refresh(issue)

    # REMEDIATE task + run
    rt_resp = client.post(
        "/tasks",
        json={"name": "Remediate", "task_type": "remediate", "data_source_id": source_id},
        headers=headers,
    )
    assert rt_resp.status_code == 201, rt_resp.text
    remediate_task_id = rt_resp.json()["id"]

    rr_resp = client.post(
        f"/tasks/{remediate_task_id}/runs",
        json={"source_task_run_id": str(detect_run_id)},
        headers=headers,
    )
    assert rr_resp.status_code == 201, rr_resp.text
    remediate_run_pk = uuid.UUID(rr_resp.json()["id"])
    remediate_task = db_session.get(Task, uuid.UUID(remediate_task_id))

    # RemediationRun (direct ORM insert)
    rrun = RemediationRun(
        organization_id=org_id,
        task_run_id=remediate_run_pk,
        task_id=remediate_task.id,
        data_source_id=uuid.UUID(source_id),
        source_task_run_id=detect_run_id,
        issues_considered_count=1,
        total_changes_count=1,
        issues_skipped_count=0,
        changes_by_action={"normalize_boolean": 1},
        skipped_by_reason={},
        remediation_engine_version="1.0.0",
    )
    db_session.add(rrun)
    db_session.commit()
    db_session.refresh(rrun)

    # RemediationChange (direct ORM insert)
    rchange = RemediationChange(
        organization_id=org_id,
        remediation_run_id=rrun.id,
        source_issue_id=issue.id,
        row_number=0,
        column_name="email",
        action="normalize_boolean",
        original_value="YES",
        proposed_value="true",
        reason="normalized true-equivalent to canonical 'true'",
        confidence=1.0,
    )
    db_session.add(rchange)
    db_session.commit()
    db_session.refresh(rchange)

    return org_id, user_id, rrun.id, rchange.id


# ---------------------------------------------------------------------------
# Section B: Model construction and persistence (basic round-trip)
# ---------------------------------------------------------------------------


def test_decision_round_trip_approved(client, db_session):
    """A RemediationChangeDecision row can be persisted and fetched."""
    org_id, user_id, run_id, change_id = _make_run_with_change(client, db_session)
    ts = datetime.now(timezone.utc)

    decision = RemediationChangeDecision(
        id=uuid.uuid4(),
        organization_id=org_id,
        remediation_run_id=run_id,
        remediation_change_id=change_id,
        decision="approved",
        reviewer_id=user_id,
        reviewer_name="Decision Reviewer",
        reviewer_role="user",
        decision_timestamp=ts,
        comment="Looks correct.",
    )
    db_session.add(decision)
    db_session.commit()

    fetched = db_session.execute(
        select(RemediationChangeDecision).where(
            RemediationChangeDecision.id == decision.id
        )
    ).scalar_one()

    assert fetched.organization_id == org_id
    assert fetched.remediation_run_id == run_id
    assert fetched.remediation_change_id == change_id
    assert fetched.decision == "approved"
    assert fetched.reviewer_id == user_id
    assert fetched.reviewer_name == "Decision Reviewer"
    assert fetched.reviewer_role == "user"
    assert fetched.comment == "Looks correct."
    assert fetched.applied_at is None
    assert fetched.applied_by is None
    assert fetched.created_at is not None


def test_decision_round_trip_rejected(client, db_session):
    """A rejected decision persists correctly."""
    org_id, user_id, run_id, change_id = _make_run_with_change(client, db_session)

    decision = RemediationChangeDecision(
        id=uuid.uuid4(),
        organization_id=org_id,
        remediation_run_id=run_id,
        remediation_change_id=change_id,
        decision="rejected",
        reviewer_id=user_id,
        reviewer_name="Decision Reviewer",
        reviewer_role="user",
        decision_timestamp=datetime.now(timezone.utc),
    )
    db_session.add(decision)
    db_session.commit()

    fetched = db_session.execute(
        select(RemediationChangeDecision).where(
            RemediationChangeDecision.id == decision.id
        )
    ).scalar_one()
    assert fetched.decision == "rejected"


# ---------------------------------------------------------------------------
# Section C: Append-only semantics (multiple rows per change)
# ---------------------------------------------------------------------------


def test_multiple_decisions_per_change_are_allowed(client, db_session):
    """No UNIQUE(organization_id, remediation_change_id) -- two rows for
    the same change must be insertable. Revision 1 core requirement."""
    org_id, user_id, run_id, change_id = _make_run_with_change(client, db_session)
    ts1 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    ts2 = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)

    d1 = RemediationChangeDecision(
        id=uuid.uuid4(),
        organization_id=org_id,
        remediation_run_id=run_id,
        remediation_change_id=change_id,
        decision="approved",
        reviewer_id=user_id,
        reviewer_name="Reviewer A",
        reviewer_role="user",
        decision_timestamp=ts1,
    )
    d2 = RemediationChangeDecision(
        id=uuid.uuid4(),
        organization_id=org_id,
        remediation_run_id=run_id,
        remediation_change_id=change_id,
        decision="rejected",
        reviewer_id=user_id,
        reviewer_name="Reviewer A",
        reviewer_role="superuser",
        decision_timestamp=ts2,
    )
    db_session.add_all([d1, d2])
    db_session.commit()  # must not raise IntegrityError

    rows = (
        db_session.execute(
            select(RemediationChangeDecision).where(
                RemediationChangeDecision.remediation_change_id == change_id
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2


def test_latest_row_is_effective_state(client, db_session):
    """The effective state is the row with the latest decision_timestamp.
    First: approved, second: rejected. Latest should be 'rejected'."""
    org_id, user_id, run_id, change_id = _make_run_with_change(client, db_session)
    ts1 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    ts2 = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)

    db_session.add_all([
        RemediationChangeDecision(
            id=uuid.uuid4(),
            organization_id=org_id,
            remediation_run_id=run_id,
            remediation_change_id=change_id,
            decision="approved",
            reviewer_id=user_id,
            reviewer_name="First",
            reviewer_role="user",
            decision_timestamp=ts1,
        ),
        RemediationChangeDecision(
            id=uuid.uuid4(),
            organization_id=org_id,
            remediation_run_id=run_id,
            remediation_change_id=change_id,
            decision="rejected",
            reviewer_id=user_id,
            reviewer_name="Second",
            reviewer_role="superuser",
            decision_timestamp=ts2,
        ),
    ])
    db_session.commit()

    effective = db_session.execute(
        select(RemediationChangeDecision)
        .where(
            RemediationChangeDecision.organization_id == org_id,
            RemediationChangeDecision.remediation_change_id == change_id,
        )
        .order_by(RemediationChangeDecision.decision_timestamp.desc())
        .limit(1)
    ).scalar_one()

    assert effective.decision == "rejected"
    assert effective.reviewer_name == "Second"


# ---------------------------------------------------------------------------
# Section D: Nullable field behavior
# ---------------------------------------------------------------------------


def test_all_nullable_fields_can_be_none(client, db_session):
    """reviewer_id, reviewer_name, reviewer_role, comment, applied_at,
    applied_by are all nullable."""
    org_id, user_id, run_id, change_id = _make_run_with_change(client, db_session)

    decision = RemediationChangeDecision(
        id=uuid.uuid4(),
        organization_id=org_id,
        remediation_run_id=run_id,
        remediation_change_id=change_id,
        decision="approved",
        reviewer_id=None,
        reviewer_name=None,
        reviewer_role=None,
        decision_timestamp=datetime.now(timezone.utc),
        comment=None,
        applied_at=None,
        applied_by=None,
    )
    db_session.add(decision)
    db_session.commit()

    fetched = db_session.execute(
        select(RemediationChangeDecision).where(
            RemediationChangeDecision.id == decision.id
        )
    ).scalar_one()
    assert fetched.reviewer_id is None
    assert fetched.reviewer_name is None
    assert fetched.reviewer_role is None
    assert fetched.comment is None
    assert fetched.applied_at is None
    assert fetched.applied_by is None


def test_applied_at_and_applied_by_remain_null_in_module_16(client, db_session):
    """Both apply fields default to NULL — reserved for the future
    Apply/Export module."""
    org_id, user_id, run_id, change_id = _make_run_with_change(client, db_session)

    decision = RemediationChangeDecision(
        id=uuid.uuid4(),
        organization_id=org_id,
        remediation_run_id=run_id,
        remediation_change_id=change_id,
        decision="approved",
        reviewer_id=user_id,
        reviewer_name="Reviewer",
        reviewer_role="user",
        decision_timestamp=datetime.now(timezone.utc),
    )
    db_session.add(decision)
    db_session.commit()

    fetched = db_session.execute(
        select(RemediationChangeDecision).where(
            RemediationChangeDecision.id == decision.id
        )
    ).scalar_one()
    assert fetched.applied_at is None
    assert fetched.applied_by is None


def test_reviewer_snapshot_survives_independently(client, db_session):
    """reviewer_name and reviewer_role are snapshot columns that survive
    FK nullification (simulating user delete with SET NULL)."""
    org_id, user_id, run_id, change_id = _make_run_with_change(client, db_session)

    decision = RemediationChangeDecision(
        id=uuid.uuid4(),
        organization_id=org_id,
        remediation_run_id=run_id,
        remediation_change_id=change_id,
        decision="approved",
        reviewer_id=user_id,
        reviewer_name="Original Name",
        reviewer_role="superuser",
        decision_timestamp=datetime.now(timezone.utc),
    )
    db_session.add(decision)
    db_session.commit()

    # Simulate FK nullification (as would happen on user delete with SET NULL)
    decision.reviewer_id = None
    db_session.commit()

    fetched = db_session.execute(
        select(RemediationChangeDecision).where(
            RemediationChangeDecision.id == decision.id
        )
    ).scalar_one()
    assert fetched.reviewer_id is None
    assert fetched.reviewer_name == "Original Name"   # snapshot intact
    assert fetched.reviewer_role == "superuser"        # snapshot intact


# ---------------------------------------------------------------------------
# Section E: Constraint enforcement
# ---------------------------------------------------------------------------


def test_invalid_decision_value_rejected(client, db_session):
    """The CHECK constraint must reject any value not in
    REMEDIATION_CHANGE_DECISION_VALUES."""
    org_id, user_id, run_id, change_id = _make_run_with_change(client, db_session)

    bad = RemediationChangeDecision(
        id=uuid.uuid4(),
        organization_id=org_id,
        remediation_run_id=run_id,
        remediation_change_id=change_id,
        decision="pending",          # NOT a valid value
        reviewer_id=user_id,
        reviewer_name="Test",
        reviewer_role="user",
        decision_timestamp=datetime.now(timezone.utc),
    )
    db_session.add(bad)
    with pytest.raises(Exception):   # IntegrityError or OperationalError on SQLite
        db_session.commit()
    db_session.rollback()


def test_uq_org_id_unique_constraint_present():
    """UNIQUE(organization_id, id) must exist for the table to be a valid
    composite FK target in future modules."""
    from sqlalchemy import UniqueConstraint

    found = any(
        isinstance(arg, UniqueConstraint)
        and set(c.name for c in arg.columns) == {"organization_id", "id"}
        for arg in RemediationChangeDecision.__table_args__
    )
    assert found, "Missing UNIQUE(organization_id, id) on RemediationChangeDecision"


def test_no_unique_constraint_on_org_change():
    """UNIQUE(organization_id, remediation_change_id) must NOT exist --
    multiple rows per change are required for append-only history (Revision 1)."""
    from sqlalchemy import UniqueConstraint

    forbidden = any(
        isinstance(arg, UniqueConstraint)
        and set(c.name for c in arg.columns) == {"organization_id", "remediation_change_id"}
        for arg in RemediationChangeDecision.__table_args__
    )
    assert not forbidden, (
        "Found a UNIQUE(organization_id, remediation_change_id) constraint -- "
        "this must NOT exist; append-only history requires multiple rows per change."
    )


# ---------------------------------------------------------------------------
# Section F: ORM vs live schema audit
# ---------------------------------------------------------------------------


def test_orm_columns_match_live_schema(db_session):
    """PRAGMA table_info (SQLite) or INFORMATION_SCHEMA (PostgreSQL) must
    agree with the ORM model on column names and nullable flags."""
    from app.db.session import engine

    with engine.connect() as conn:
        if engine.dialect.name == "sqlite":
            rows = conn.execute(
                text("PRAGMA table_info(remediation_change_decisions)")
            ).fetchall()
            # rows: (cid, name, type, notnull, dflt_value, pk)
            live_cols = {r[1]: {"notnull": bool(r[3]), "pk": bool(r[5])} for r in rows}
        else:
            rows = conn.execute(
                text(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'remediation_change_decisions'"
                )
            ).fetchall()
            live_cols = {r[0]: {"notnull": r[1] == "NO"} for r in rows}

    expected_columns = {
        "id", "organization_id", "remediation_run_id", "remediation_change_id",
        "decision", "reviewer_id", "reviewer_name", "reviewer_role",
        "decision_timestamp", "comment", "applied_at", "applied_by", "created_at",
    }

    assert set(live_cols.keys()) == expected_columns, (
        f"Column mismatch.\n"
        f"  ORM expects:  {sorted(expected_columns)}\n"
        f"  Live has:     {sorted(live_cols.keys())}"
    )

    # Required NOT NULL columns
    for col in ("id", "organization_id", "remediation_run_id",
                "remediation_change_id", "decision", "decision_timestamp",
                "created_at"):
        assert live_cols[col]["notnull"], f"Column '{col}' must be NOT NULL in live schema"

    # Required nullable columns
    for col in ("reviewer_id", "reviewer_name", "reviewer_role",
                "comment", "applied_at", "applied_by"):
        assert not live_cols[col]["notnull"], f"Column '{col}' must be nullable in live schema"


def test_composite_index_on_org_change_ts_exists(db_session):
    """The composite index (organization_id, remediation_change_id,
    decision_timestamp) must exist for efficient "latest row" queries."""
    from app.db.session import engine

    inspector = inspect(engine)
    indexes = inspector.get_indexes("remediation_change_decisions")
    index_names = {idx["name"] for idx in indexes}
    assert "ix_remediation_change_decisions_org_chg_ts" in index_names, (
        f"Composite index not found. Available indexes: {index_names}"
    )
