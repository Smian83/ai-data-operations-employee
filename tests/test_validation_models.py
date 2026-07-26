"""Module 17 Phase 1 tests: ValidationRun and ValidationResult ORM models,
enum/constant consistency, constraint enforcement, and all four approved
architecture adjustments.

No API endpoints exist yet (Phase 2+). All tests work directly against the
ORM and database layer. Tests pass on both SQLite (sandbox) and PostgreSQL
(real-Postgres verification).

Sections:
  A. Enum/constant consistency (VALIDATION_OUTCOMES, VALIDATION_RULE_NAMES,
     TaskType.VALIDATE)
  B. Adjustment 2 — validation_rule_version column present on every result
  C. Adjustment 3 — ix_validation_results_outcome index exists
  D. Adjustment 4 — reason / text columns are Text() (encryption compat)
  E. ValidationRun round-trip + count-reconcile CHECK
  F. ValidationResult round-trip + outcome CHECK
  G. Idempotency: UNIQUE(task_run_id) on validation_runs
  H. Append-only semantics: NO UNIQUE(org_id, remediation_change_id)
  I. Composite FK cascade: deleting a ValidationRun cascades to results
  J. ORM vs live schema audit (column names + nullable flags)
  K. Identifier length audit (all named constraints/indexes ≤ 63 bytes)
"""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import CheckConstraint, Index, UniqueConstraint, inspect, select, text

from app.models.enums import (
    REMEDIATION_ACTIONS,
    VALIDATION_OUTCOMES,
    VALIDATION_RULE_NAMES,
    TaskType,
)
from app.models.issue import Issue
from app.models.issue_detection_run import IssueDetectionRun
from app.models.remediation_change import RemediationChange
from app.models.remediation_run import RemediationRun
from app.models.task import Task
from app.models.validation_result import ValidationResult
from app.models.validation_run import ValidationRun


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

ENGINE_VERSION = "17.0.0"
RULE_VERSION = "1.0"


def _build_prerequisite_chain(db_session, client):
    """Build the full prerequisite chain required to insert a ValidationRun:
    org + user → data source → DETECT task/run → IssueDetectionRun + Issue
    → REMEDIATE task/run → RemediationRun + RemediationChange
    → VALIDATE task/run.

    Returns a dict with all IDs needed by callers.

    Pattern follows test_remediation_decisions.py::_make_run_with_change,
    extended with the VALIDATE task/run on top. FK enforcement in SQLite
    requires every referenced row to actually exist in the database."""
    suffix = uuid.uuid4().hex[:8]
    resp = client.post(
        "/auth/register",
        json={
            "organization_name": f"Validation Org {suffix}",
            "email": f"val-{suffix}@example.com",
            "password": "correct-horse-battery-17",
            "full_name": "Validation Tester",
        },
    )
    assert resp.status_code == 201, resp.text
    headers = {"Authorization": f"Bearer {resp.json()['access_token']}"}

    src_resp = client.post(
        "/data-sources",
        json={
            "name": f"DS-Val-{suffix}",
            "source_type": "csv_upload",
            "connection_metadata": {"file_path": "test.csv"},
        },
        headers=headers,
    )
    assert src_resp.status_code == 201, src_resp.text
    source_id = uuid.UUID(src_resp.json()["id"])
    org_id = uuid.UUID(src_resp.json()["organization_id"])

    # ---- DETECT task + run ----
    dt_resp = client.post(
        "/tasks",
        json={"name": f"Detect-{suffix}", "task_type": "detect",
              "data_source_id": str(source_id)},
        headers=headers,
    )
    assert dt_resp.status_code == 201, dt_resp.text
    detect_task_id = uuid.UUID(dt_resp.json()["id"])

    dr_resp = client.post(f"/tasks/{detect_task_id}/runs", headers=headers)
    assert dr_resp.status_code == 201, dr_resp.text
    detect_run_id = uuid.UUID(dr_resp.json()["id"])

    detect_task = db_session.get(Task, detect_task_id)

    # ---- IssueDetectionRun + Issue (direct ORM) ----
    idr = IssueDetectionRun(
        organization_id=org_id,
        task_run_id=detect_run_id,
        task_id=detect_task.id,
        data_source_id=source_id,
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

    # ---- REMEDIATE task + run ----
    rt_resp = client.post(
        "/tasks",
        json={"name": f"Remediate-{suffix}", "task_type": "remediate",
              "data_source_id": str(source_id)},
        headers=headers,
    )
    assert rt_resp.status_code == 201, rt_resp.text
    remediate_task_id = uuid.UUID(rt_resp.json()["id"])

    rr_resp = client.post(
        f"/tasks/{remediate_task_id}/runs",
        json={"source_task_run_id": str(detect_run_id)},
        headers=headers,
    )
    assert rr_resp.status_code == 201, rr_resp.text
    remediate_run_pk = uuid.UUID(rr_resp.json()["id"])
    remediate_task = db_session.get(Task, remediate_task_id)

    # ---- RemediationRun + RemediationChange (direct ORM) ----
    rrun = RemediationRun(
        organization_id=org_id,
        task_run_id=remediate_run_pk,
        task_id=remediate_task.id,
        data_source_id=source_id,
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

    # ---- VALIDATE task + run ----
    vt_resp = client.post(
        "/tasks",
        json={"name": f"Validate-{suffix}", "task_type": "validate",
              "data_source_id": str(source_id)},
        headers=headers,
    )
    assert vt_resp.status_code == 201, vt_resp.text
    validate_task_id = uuid.UUID(vt_resp.json()["id"])

    vtr_resp = client.post(
        f"/tasks/{validate_task_id}/runs",
        json={"source_task_run_id": str(remediate_run_pk)},
        headers=headers,
    )
    assert vtr_resp.status_code == 201, vtr_resp.text
    validate_run_id = uuid.UUID(vtr_resp.json()["id"])

    return {
        "org_id": org_id,
        "source_id": source_id,
        "validate_task_id": validate_task_id,
        "validate_task_run_id": validate_run_id,
        "remediation_run_id": rrun.id,
        "remediation_change_id": rchange.id,
        "source_issue_id": issue.id,
    }


def _insert_validation_run(
    db_session,
    *,
    org_id: uuid.UUID,
    task_run_id: uuid.UUID,
    task_id: uuid.UUID,
    data_source_id: uuid.UUID,
    remediation_run_id: uuid.UUID,
    approved: int = 3,
    passed: int = 2,
    failed: int = 1,
    skipped: int = 0,
) -> ValidationRun:
    """Insert a ValidationRun with count-reconcile invariant satisfied by
    default (passed + failed + skipped == approved)."""
    run = ValidationRun(
        id=uuid.uuid4(),
        organization_id=org_id,
        task_run_id=task_run_id,
        task_id=task_id,
        data_source_id=data_source_id,
        remediation_run_id=remediation_run_id,
        approved_changes_considered=approved,
        passed_count=passed,
        failed_count=failed,
        skipped_count=skipped,
        results_by_rule={
            "validate_trim_whitespace": {"passed": 2, "failed": 1, "skipped": 0}
        },
        validation_engine_version=ENGINE_VERSION,
    )
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


def _insert_validation_result(
    db_session,
    *,
    org_id: uuid.UUID,
    validation_run: ValidationRun,
    remediation_change_id: uuid.UUID,
    source_issue_id: uuid.UUID,
    outcome: str = "passed",
    rule: str = "validate_trim_whitespace",
    rule_version: str = RULE_VERSION,
) -> ValidationResult:
    result = ValidationResult(
        id=uuid.uuid4(),
        organization_id=org_id,
        validation_run_id=validation_run.id,
        remediation_run_id=validation_run.remediation_run_id,
        remediation_change_id=remediation_change_id,
        source_issue_id=source_issue_id,
        validation_rule=rule,
        outcome=outcome,
        reason="trim_whitespace: proposed value matches stripped original",
        original_value="  hello  ",
        proposed_value="hello",
        validation_engine_version=ENGINE_VERSION,
        validation_rule_version=rule_version,  # Adjustment 2
    )
    db_session.add(result)
    db_session.commit()
    db_session.refresh(result)
    return result


# ---------------------------------------------------------------------------
# Section A: Enum/constant consistency
# ---------------------------------------------------------------------------


def test_validation_outcomes_has_exactly_three_values():
    assert len(VALIDATION_OUTCOMES) == 3


def test_validation_outcomes_contains_passed_failed_skipped():
    assert "passed" in VALIDATION_OUTCOMES
    assert "failed" in VALIDATION_OUTCOMES
    assert "skipped" in VALIDATION_OUTCOMES


def test_validation_outcomes_are_unique():
    assert len(set(VALIDATION_OUTCOMES)) == len(VALIDATION_OUTCOMES)


def test_validation_outcomes_all_fit_in_varchar_10():
    for v in VALIDATION_OUTCOMES:
        assert len(v) <= 10, f"'{v}' exceeds VARCHAR(10)"


def test_validation_rule_names_count_equals_remediation_actions():
    """One validation rule per remediation action — counts must match."""
    assert len(VALIDATION_RULE_NAMES) == len(REMEDIATION_ACTIONS) == 10


def test_validation_rule_names_are_unique():
    assert len(set(VALIDATION_RULE_NAMES)) == len(VALIDATION_RULE_NAMES)


def test_validation_rule_names_all_fit_in_varchar_50():
    for name in VALIDATION_RULE_NAMES:
        assert len(name) <= 50, f"'{name}' exceeds VARCHAR(50)"


def test_task_type_validate_value_exists():
    """TaskType.VALIDATE must be present — it is the enum value that routes
    a TaskRun to the ValidationHandler."""
    assert TaskType.VALIDATE.value == "validate"


def test_task_type_validate_in_task_type_enum():
    values = [e.value for e in TaskType]
    assert "validate" in values


# ---------------------------------------------------------------------------
# Section B: Adjustment 2 — validation_rule_version per-rule column
# ---------------------------------------------------------------------------


def test_validation_rule_version_column_exists_on_result():
    """ValidationResult must have a validation_rule_version mapped column."""
    columns = {c.name for c in ValidationResult.__table__.columns}
    assert "validation_rule_version" in columns


def test_validation_rule_version_is_not_nullable():
    col = ValidationResult.__table__.columns["validation_rule_version"]
    assert not col.nullable, "validation_rule_version must be NOT NULL"


def test_two_results_can_have_different_rule_versions(client, db_session):
    """Two results in the same run can carry different rule_version values --
    per-rule independent bumping (Adjustment 2) must work at the DB level."""
    chain = _build_prerequisite_chain(db_session, client)
    vrun = _insert_validation_run(
        db_session,
        org_id=chain["org_id"],
        task_run_id=chain["validate_task_run_id"],
        task_id=chain["validate_task_id"],
        data_source_id=chain["source_id"],
        remediation_run_id=chain["remediation_run_id"],
    )

    r1 = _insert_validation_result(
        db_session, org_id=chain["org_id"], validation_run=vrun,
        remediation_change_id=chain["remediation_change_id"],
        source_issue_id=chain["source_issue_id"],
        rule_version="1.0",
    )
    r2 = _insert_validation_result(
        db_session, org_id=chain["org_id"], validation_run=vrun,
        remediation_change_id=chain["remediation_change_id"],
        source_issue_id=chain["source_issue_id"],
        rule_version="2.0",
    )

    f1 = db_session.get(ValidationResult, r1.id)
    f2 = db_session.get(ValidationResult, r2.id)
    assert f1.validation_rule_version == "1.0"
    assert f2.validation_rule_version == "2.0"
    assert f1.validation_run_id == f2.validation_run_id  # same run


# ---------------------------------------------------------------------------
# Section C: Adjustment 3 — ix_validation_results_outcome index
# ---------------------------------------------------------------------------


def test_ix_validation_results_outcome_exists_in_orm():
    """The outcome column must have index=True (ix_validation_results_outcome)
    set on its Column definition (Adjustment 3)."""
    col = ValidationResult.__table__.columns["outcome"]
    assert col.index, (
        "ValidationResult.outcome must have index=True (Adjustment 3: "
        "ix_validation_results_outcome)"
    )


def test_ix_validation_results_outcome_exists_in_live_schema(db_session):
    """The named index must be present in the live database schema."""
    from app.db.session import engine

    inspector = inspect(engine)
    indexes = inspector.get_indexes("validation_results")
    index_names = {idx["name"] for idx in indexes}
    assert "ix_validation_results_outcome" in index_names, (
        f"ix_validation_results_outcome not found. Available: {index_names}"
    )


# ---------------------------------------------------------------------------
# Section D: Adjustment 4 — Text() columns (encryption compatibility)
# ---------------------------------------------------------------------------


def test_reason_column_is_text_not_varchar():
    """reason must be Text() (unbounded) — Adjustment 4: no schema change
    required when future field-level encryption is added."""
    from sqlalchemy import Text as SAText

    col = ValidationResult.__table__.columns["reason"]
    assert isinstance(col.type, SAText), (
        f"reason column is {type(col.type).__name__}, expected Text()"
    )


def test_original_value_column_is_text_not_varchar():
    from sqlalchemy import Text as SAText
    col = ValidationResult.__table__.columns["original_value"]
    assert isinstance(col.type, SAText), (
        f"original_value column is {type(col.type).__name__}, expected Text()"
    )


def test_proposed_value_column_is_text_not_varchar():
    from sqlalchemy import Text as SAText
    col = ValidationResult.__table__.columns["proposed_value"]
    assert isinstance(col.type, SAText), (
        f"proposed_value column is {type(col.type).__name__}, expected Text()"
    )


# ---------------------------------------------------------------------------
# Section E: ValidationRun round-trip + count-reconcile CHECK
# ---------------------------------------------------------------------------


def test_validation_run_round_trip(client, db_session):
    chain = _build_prerequisite_chain(db_session, client)

    vrun = _insert_validation_run(
        db_session,
        org_id=chain["org_id"],
        task_run_id=chain["validate_task_run_id"],
        task_id=chain["validate_task_id"],
        data_source_id=chain["source_id"],
        remediation_run_id=chain["remediation_run_id"],
        approved=5,
        passed=3,
        failed=1,
        skipped=1,
    )

    fetched = db_session.get(ValidationRun, vrun.id)
    assert fetched is not None
    assert fetched.organization_id == chain["org_id"]
    assert fetched.task_run_id == chain["validate_task_run_id"]
    assert fetched.approved_changes_considered == 5
    assert fetched.passed_count == 3
    assert fetched.failed_count == 1
    assert fetched.skipped_count == 1
    assert fetched.validation_engine_version == ENGINE_VERSION
    assert fetched.created_at is not None


def test_all_zero_counts_satisfy_reconcile_check(client, db_session):
    """approved=0, passed=0, failed=0, skipped=0 must satisfy the CHECK."""
    chain = _build_prerequisite_chain(db_session, client)

    vrun = _insert_validation_run(
        db_session,
        org_id=chain["org_id"],
        task_run_id=chain["validate_task_run_id"],
        task_id=chain["validate_task_id"],
        data_source_id=chain["source_id"],
        remediation_run_id=chain["remediation_run_id"],
        approved=0,
        passed=0,
        failed=0,
        skipped=0,
    )
    fetched = db_session.get(ValidationRun, vrun.id)
    assert fetched.approved_changes_considered == 0


def test_count_reconcile_check_rejects_mismatch(client, db_session):
    """passed + failed + skipped != approved must be rejected by the CHECK
    constraint (ck_validation_runs_counts_reconcile)."""
    chain = _build_prerequisite_chain(db_session, client)

    bad_run = ValidationRun(
        id=uuid.uuid4(),
        organization_id=chain["org_id"],
        task_run_id=chain["validate_task_run_id"],
        task_id=chain["validate_task_id"],
        data_source_id=chain["source_id"],
        remediation_run_id=chain["remediation_run_id"],
        approved_changes_considered=10,
        passed_count=3,    # 3 + 3 + 3 = 9 ≠ 10
        failed_count=3,
        skipped_count=3,
        results_by_rule={},
        validation_engine_version=ENGINE_VERSION,
    )
    db_session.add(bad_run)
    with pytest.raises(Exception):   # IntegrityError or OperationalError
        db_session.commit()
    db_session.rollback()


def test_negative_passed_count_rejected(client, db_session):
    """ck_validation_runs_passed_nonneg must reject passed_count < 0."""
    chain = _build_prerequisite_chain(db_session, client)

    bad = ValidationRun(
        id=uuid.uuid4(),
        organization_id=chain["org_id"],
        task_run_id=chain["validate_task_run_id"],
        task_id=chain["validate_task_id"],
        data_source_id=chain["source_id"],
        remediation_run_id=chain["remediation_run_id"],
        approved_changes_considered=0,
        passed_count=-1,
        failed_count=0,
        skipped_count=1,   # still sums to approved (0), but passed is negative
        results_by_rule={},
        validation_engine_version=ENGINE_VERSION,
    )
    db_session.add(bad)
    with pytest.raises(Exception):
        db_session.commit()
    db_session.rollback()


def test_uq_validation_runs_task_run_id_present():
    """UNIQUE(task_run_id) must exist for idempotency enforcement."""
    found = any(
        isinstance(arg, UniqueConstraint)
        and {c.name for c in arg.columns} == {"task_run_id"}
        for arg in ValidationRun.__table_args__
    )
    assert found, "Missing UNIQUE(task_run_id) on ValidationRun"


# ---------------------------------------------------------------------------
# Section F: ValidationResult round-trip + outcome CHECK
# ---------------------------------------------------------------------------


def test_validation_result_round_trip(client, db_session):
    chain = _build_prerequisite_chain(db_session, client)
    vrun = _insert_validation_run(
        db_session,
        org_id=chain["org_id"],
        task_run_id=chain["validate_task_run_id"],
        task_id=chain["validate_task_id"],
        data_source_id=chain["source_id"],
        remediation_run_id=chain["remediation_run_id"],
    )

    result = _insert_validation_result(
        db_session,
        org_id=chain["org_id"],
        validation_run=vrun,
        remediation_change_id=chain["remediation_change_id"],
        source_issue_id=chain["source_issue_id"],
        outcome="failed",
    )

    fetched = db_session.get(ValidationResult, result.id)
    assert fetched is not None
    assert fetched.organization_id == chain["org_id"]
    assert fetched.validation_run_id == vrun.id
    assert fetched.outcome == "failed"
    assert fetched.reason == "trim_whitespace: proposed value matches stripped original"
    assert fetched.original_value == "  hello  "
    assert fetched.proposed_value == "hello"
    assert fetched.validation_engine_version == ENGINE_VERSION
    assert fetched.validation_rule_version == RULE_VERSION
    assert fetched.created_at is not None


def test_invalid_outcome_rejected(client, db_session):
    """ck_validation_results_outcome_valid must reject any value not in
    VALIDATION_OUTCOMES."""
    chain = _build_prerequisite_chain(db_session, client)
    vrun = _insert_validation_run(
        db_session,
        org_id=chain["org_id"],
        task_run_id=chain["validate_task_run_id"],
        task_id=chain["validate_task_id"],
        data_source_id=chain["source_id"],
        remediation_run_id=chain["remediation_run_id"],
    )

    bad = ValidationResult(
        id=uuid.uuid4(),
        organization_id=chain["org_id"],
        validation_run_id=vrun.id,
        remediation_run_id=vrun.remediation_run_id,
        remediation_change_id=chain["remediation_change_id"],
        source_issue_id=chain["source_issue_id"],
        validation_rule="validate_trim_whitespace",
        outcome="unknown",           # NOT a valid VALIDATION_OUTCOMES value
        reason="test",
        validation_engine_version=ENGINE_VERSION,
        validation_rule_version=RULE_VERSION,
    )
    db_session.add(bad)
    with pytest.raises(Exception):
        db_session.commit()
    db_session.rollback()


def test_all_three_outcome_values_accepted(client, db_session):
    """All three values in VALIDATION_OUTCOMES must be individually accepted
    by the CHECK constraint."""
    chain = _build_prerequisite_chain(db_session, client)
    vrun = _insert_validation_run(
        db_session,
        org_id=chain["org_id"],
        task_run_id=chain["validate_task_run_id"],
        task_id=chain["validate_task_id"],
        data_source_id=chain["source_id"],
        remediation_run_id=chain["remediation_run_id"],
        approved=3,
        passed=1,
        failed=1,
        skipped=1,
    )

    for outcome in VALIDATION_OUTCOMES:
        r = _insert_validation_result(
            db_session,
            org_id=chain["org_id"],
            validation_run=vrun,
            remediation_change_id=chain["remediation_change_id"],
            source_issue_id=chain["source_issue_id"],
            outcome=outcome,
        )
        fetched = db_session.get(ValidationResult, r.id)
        assert fetched.outcome == outcome


def test_original_value_and_proposed_value_can_be_none(client, db_session):
    """Both columns are nullable — removal actions produce no single value."""
    chain = _build_prerequisite_chain(db_session, client)
    vrun = _insert_validation_run(
        db_session,
        org_id=chain["org_id"],
        task_run_id=chain["validate_task_run_id"],
        task_id=chain["validate_task_id"],
        data_source_id=chain["source_id"],
        remediation_run_id=chain["remediation_run_id"],
    )

    result = ValidationResult(
        id=uuid.uuid4(),
        organization_id=chain["org_id"],
        validation_run_id=vrun.id,
        remediation_run_id=vrun.remediation_run_id,
        remediation_change_id=chain["remediation_change_id"],
        source_issue_id=chain["source_issue_id"],
        validation_rule="validate_remove_duplicate_row",
        outcome="passed",
        reason="row is a confirmed duplicate",
        original_value=None,
        proposed_value=None,
        validation_engine_version=ENGINE_VERSION,
        validation_rule_version=RULE_VERSION,
    )
    db_session.add(result)
    db_session.commit()

    fetched = db_session.get(ValidationResult, result.id)
    assert fetched.original_value is None
    assert fetched.proposed_value is None


# ---------------------------------------------------------------------------
# Section G: Idempotency — UNIQUE(task_run_id) on validation_runs
# ---------------------------------------------------------------------------


def test_duplicate_task_run_id_rejected(client, db_session):
    """Two ValidationRun rows with the same task_run_id must be rejected by
    uq_validation_runs_task_run_id (idempotency enforcement)."""
    chain = _build_prerequisite_chain(db_session, client)

    _insert_validation_run(
        db_session,
        org_id=chain["org_id"],
        task_run_id=chain["validate_task_run_id"],
        task_id=chain["validate_task_id"],
        data_source_id=chain["source_id"],
        remediation_run_id=chain["remediation_run_id"],
    )

    duplicate = ValidationRun(
        id=uuid.uuid4(),
        organization_id=chain["org_id"],
        task_run_id=chain["validate_task_run_id"],  # same task_run_id — must be rejected
        task_id=chain["validate_task_id"],
        data_source_id=chain["source_id"],
        remediation_run_id=chain["remediation_run_id"],
        approved_changes_considered=0,
        passed_count=0,
        failed_count=0,
        skipped_count=0,
        results_by_rule={},
        validation_engine_version=ENGINE_VERSION,
    )
    db_session.add(duplicate)
    with pytest.raises(Exception):   # IntegrityError
        db_session.commit()
    db_session.rollback()


# ---------------------------------------------------------------------------
# Section H: Append-only semantics — NO UNIQUE on (org_id, change_id)
# ---------------------------------------------------------------------------


def test_no_unique_on_org_remediation_change_in_validation_runs():
    """Deliberate omission: ValidationRun has no unique constraint on
    (organization_id, remediation_run_id), allowing re-validation runs."""
    forbidden = any(
        isinstance(arg, UniqueConstraint)
        and {c.name for c in arg.columns} == {"organization_id", "remediation_run_id"}
        for arg in ValidationRun.__table_args__
    )
    assert not forbidden, (
        "Found UNIQUE(organization_id, remediation_run_id) on ValidationRun — "
        "this must NOT exist; multiple runs per remediation run are allowed."
    )


def test_no_unique_on_org_change_in_validation_results():
    """Deliberate omission: ValidationResult has no UNIQUE(organization_id,
    remediation_change_id) — a future re-validation pass must be able to
    insert new rows without erasing history."""
    forbidden = any(
        isinstance(arg, UniqueConstraint)
        and {c.name for c in arg.columns} == {"organization_id", "remediation_change_id"}
        for arg in ValidationResult.__table_args__
    )
    assert not forbidden, (
        "Found UNIQUE(organization_id, remediation_change_id) on ValidationResult — "
        "this must NOT exist; append-only re-validation requires multiple rows per change."
    )


def test_multiple_results_for_same_change_accepted(client, db_session):
    """Two ValidationResult rows for the same remediation_change_id in the
    same run must both be accepted (no unique constraint blocks them)."""
    chain = _build_prerequisite_chain(db_session, client)
    vrun = _insert_validation_run(
        db_session,
        org_id=chain["org_id"],
        task_run_id=chain["validate_task_run_id"],
        task_id=chain["validate_task_id"],
        data_source_id=chain["source_id"],
        remediation_run_id=chain["remediation_run_id"],
        approved=2,
        passed=2,
        failed=0,
        skipped=0,
    )

    # Use the real remediation_change_id for both rows — the FK must accept it
    # twice, proving no UNIQUE(org, remediation_change_id) is enforced.
    change_id = chain["remediation_change_id"]
    for outcome in ("passed", "passed"):
        r = ValidationResult(
            id=uuid.uuid4(),
            organization_id=chain["org_id"],
            validation_run_id=vrun.id,
            remediation_run_id=vrun.remediation_run_id,
            remediation_change_id=change_id,   # same change_id both times
            source_issue_id=chain["source_issue_id"],
            validation_rule="validate_trim_whitespace",
            outcome=outcome,
            reason="re-validation test",
            validation_engine_version=ENGINE_VERSION,
            validation_rule_version=RULE_VERSION,
        )
        db_session.add(r)
    db_session.commit()  # must not raise IntegrityError

    rows = db_session.execute(
        select(ValidationResult).where(
            ValidationResult.remediation_change_id == change_id
        )
    ).scalars().all()
    assert len(rows) == 2


# ---------------------------------------------------------------------------
# Section I: Cascade — deleting a ValidationRun cascades to its results
# ---------------------------------------------------------------------------


def test_delete_validation_run_cascades_to_results(client, db_session):
    """ValidationResult rows have ondelete=CASCADE via the composite FK
    fk_validation_results_org_validation_run. Deleting the parent run must
    also delete all child result rows."""
    chain = _build_prerequisite_chain(db_session, client)
    vrun = _insert_validation_run(
        db_session,
        org_id=chain["org_id"],
        task_run_id=chain["validate_task_run_id"],
        task_id=chain["validate_task_id"],
        data_source_id=chain["source_id"],
        remediation_run_id=chain["remediation_run_id"],
        approved=2,
        passed=1,
        failed=1,
        skipped=0,
    )
    r1 = _insert_validation_result(
        db_session, org_id=chain["org_id"], validation_run=vrun,
        remediation_change_id=chain["remediation_change_id"],
        source_issue_id=chain["source_issue_id"],
        outcome="passed",
    )
    r2 = _insert_validation_result(
        db_session, org_id=chain["org_id"], validation_run=vrun,
        remediation_change_id=chain["remediation_change_id"],
        source_issue_id=chain["source_issue_id"],
        outcome="failed",
    )
    run_id = vrun.id
    r1_id = r1.id
    r2_id = r2.id

    db_session.delete(vrun)
    db_session.commit()

    assert db_session.get(ValidationRun, run_id) is None
    # Results must have cascaded away
    assert db_session.get(ValidationResult, r1_id) is None
    assert db_session.get(ValidationResult, r2_id) is None


# ---------------------------------------------------------------------------
# Section J: ORM vs live schema audit
# ---------------------------------------------------------------------------


def test_validation_runs_live_schema_columns(db_session):
    """validation_runs must have exactly the 13 columns defined in the ORM
    model, with correct nullable flags."""
    from app.db.session import engine

    with engine.connect() as conn:
        if engine.dialect.name == "sqlite":
            rows = conn.execute(text("PRAGMA table_info(validation_runs)")).fetchall()
            live_cols = {r[1]: {"notnull": bool(r[3])} for r in rows}
        else:
            rows = conn.execute(
                text(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'validation_runs'"
                )
            ).fetchall()
            live_cols = {r[0]: {"notnull": r[1] == "NO"} for r in rows}

    expected = {
        "id", "organization_id", "task_run_id", "task_id", "data_source_id",
        "remediation_run_id", "approved_changes_considered", "passed_count",
        "failed_count", "skipped_count", "results_by_rule",
        "validation_engine_version", "created_at",
    }
    assert set(live_cols.keys()) == expected, (
        f"Column mismatch on validation_runs.\n"
        f"  Expected: {sorted(expected)}\n"
        f"  Got:      {sorted(live_cols.keys())}"
    )

    for col in expected - {"created_at"}:  # created_at has a server_default
        # All columns in validation_runs are NOT NULL
        assert live_cols[col]["notnull"], f"Column '{col}' must be NOT NULL"


def test_validation_results_live_schema_columns(db_session):
    """validation_results must have exactly the 14 columns defined in the ORM
    model, with correct nullable flags."""
    from app.db.session import engine

    with engine.connect() as conn:
        if engine.dialect.name == "sqlite":
            rows = conn.execute(text("PRAGMA table_info(validation_results)")).fetchall()
            live_cols = {r[1]: {"notnull": bool(r[3])} for r in rows}
        else:
            rows = conn.execute(
                text(
                    "SELECT column_name, is_nullable FROM information_schema.columns "
                    "WHERE table_name = 'validation_results'"
                )
            ).fetchall()
            live_cols = {r[0]: {"notnull": r[1] == "NO"} for r in rows}

    expected = {
        "id", "organization_id", "validation_run_id", "remediation_run_id",
        "remediation_change_id", "source_issue_id", "validation_rule",
        "outcome", "reason", "original_value", "proposed_value",
        "validation_engine_version", "validation_rule_version", "created_at",
    }
    assert set(live_cols.keys()) == expected, (
        f"Column mismatch on validation_results.\n"
        f"  Expected: {sorted(expected)}\n"
        f"  Got:      {sorted(live_cols.keys())}"
    )

    non_nullable = {
        "id", "organization_id", "validation_run_id", "remediation_run_id",
        "remediation_change_id", "source_issue_id", "validation_rule",
        "outcome", "reason", "validation_engine_version",
        "validation_rule_version",
    }
    nullable = {"original_value", "proposed_value"}

    for col in non_nullable:
        assert live_cols[col]["notnull"], f"Column '{col}' must be NOT NULL"
    for col in nullable:
        assert not live_cols[col]["notnull"], f"Column '{col}' must be nullable"


def test_composite_index_org_change_ts_on_results(db_session):
    """ix_validation_results_org_change_ts must exist in the live schema."""
    from app.db.session import engine

    inspector = inspect(engine)
    indexes = inspector.get_indexes("validation_results")
    index_names = {idx["name"] for idx in indexes}
    assert "ix_validation_results_org_change_ts" in index_names, (
        f"ix_validation_results_org_change_ts not found. Available: {index_names}"
    )


# ---------------------------------------------------------------------------
# Section K: Identifier length audit (≤ 63 bytes, PostgreSQL NAMEDATALEN)
# ---------------------------------------------------------------------------


def _collect_identifier_names(table_class) -> list[str]:
    names = []
    for arg in table_class.__table_args__:
        if isinstance(arg, (CheckConstraint, UniqueConstraint)):
            if hasattr(arg, "name") and arg.name:
                names.append(arg.name)
        elif isinstance(arg, Index):
            if arg.name:
                names.append(arg.name)
        else:
            # ForeignKeyConstraint
            if hasattr(arg, "name") and arg.name:
                names.append(arg.name)
    for col in table_class.__table__.columns:
        for fk in col.foreign_keys:
            if hasattr(fk.constraint, "name") and fk.constraint.name:
                names.append(fk.constraint.name)
    return names


def test_validation_run_identifier_names_within_namedatalen():
    names = _collect_identifier_names(ValidationRun)
    assert names, "No named constraints/indexes found on ValidationRun"
    for name in names:
        assert len(name.encode()) <= 63, (
            f"Identifier '{name}' is {len(name.encode())} bytes — "
            f"exceeds PostgreSQL's 63-byte NAMEDATALEN limit"
        )


def test_validation_result_identifier_names_within_namedatalen():
    names = _collect_identifier_names(ValidationResult)
    assert names, "No named constraints/indexes found on ValidationResult"
    for name in names:
        assert len(name.encode()) <= 63, (
            f"Identifier '{name}' is {len(name.encode())} bytes — "
            f"exceeds PostgreSQL's 63-byte NAMEDATALEN limit"
        )
