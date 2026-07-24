"""Module 14 Phase 1 tests: the new task_type_enum value ('detect') and the
three new tables (issue_detection_runs, issues, issue_detection_column_rules)
-- models and CHECK/FK/unique-index constraints only. No detection engine or
handler exists yet (see app.models.issue_detection_run/issue/
issue_detection_column_rule's own docstrings), so a TaskRun built here is
only ever exercised through the generic task/run-creation API, never
executed -- exactly like test_artifact_download_constraints.py builds a
valid parent row via the real pipeline, then attempts a series of invalid
child inserts directly against the session, each expected to raise
IntegrityError.

Runs against whatever DATABASE_URL the suite is pointed at -- SQLite in the
sandbox, real PostgreSQL during the dedicated verification pass -- since
both backends enforce CHECK/unique constraints identically."""
import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import IntegrityError

from app.models.data_source import DataSource
from app.models.enums import ISSUE_DETECTION_EXPECTED_TYPES, ISSUE_SEVERITIES, ISSUE_TYPES, TaskType
from app.models.issue import Issue
from app.models.issue_detection_column_rule import IssueDetectionColumnRule
from app.models.issue_detection_run import IssueDetectionRun
from app.models.task import Task
from app.models.task_run import TaskRun


def _auth_headers(client: TestClient, suffix: str) -> dict:
    response = client.post(
        "/auth/register",
        json={
            "organization_name": f"Detection Models Org {suffix}",
            "email": f"detection-models-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "Detection Models User",
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _build_detect_task_run(client: TestClient, db_session, suffix: str) -> TaskRun:
    """A DataSource -> Task(task_type='detect') -> TaskRun chain built
    entirely through the existing generic API, left in its default
    'pending' status -- sufficient for every FK in this file, since no
    IssueDetectionHandler exists yet to actually execute it."""
    headers = _auth_headers(client, suffix)
    source_id = client.post(
        "/data-sources",
        json={
            "name": "Detection Source", "source_type": "csv_upload",
            "connection_metadata": {"file_path": "customers.csv"},
        },
        headers=headers,
    ).json()["id"]
    task_response = client.post(
        "/tasks",
        json={"name": "Detect", "task_type": "detect", "data_source_id": source_id},
        headers=headers,
    )
    assert task_response.status_code == 201, task_response.text
    assert task_response.json()["task_type"] == "detect"
    task_id = task_response.json()["id"]
    run_response = client.post(f"/tasks/{task_id}/runs", headers=headers)
    assert run_response.status_code == 201, run_response.text
    return db_session.get(TaskRun, uuid.UUID(run_response.json()["id"]))


def _base_run_kwargs(task_run: TaskRun) -> dict:
    """A minimally valid IssueDetectionRun's constructor kwargs -- each
    constraint test below mutates exactly one field to an invalid value."""
    return dict(
        id=uuid.uuid4(),
        organization_id=task_run.organization_id,
        task_run_id=task_run.id,
        task_id=task_run.task_id,
        data_source_id=task_run.task.data_source_id,
        rows_scanned=100,
        columns_scanned=3,
        total_issues_found=5,
        persisted_issue_count=5,
        issues_by_severity={"INFO": 2, "LOW": 1, "MEDIUM": 1, "HIGH": 1, "CRITICAL": 0},
        issues_by_type={"empty_string": 3, "invalid_email": 2},
        limits_applied={"issue_detection_max_persisted_issues": 10_000},
        detection_engine_version="1.0",
    )


@pytest.fixture
def detect_task_run(client: TestClient, db_session) -> TaskRun:
    return _build_detect_task_run(client, db_session, uuid.uuid4().hex)


@pytest.fixture
def valid_detection_run(db_session, detect_task_run: TaskRun) -> IssueDetectionRun:
    run = IssueDetectionRun(**_base_run_kwargs(detect_task_run))
    db_session.add(run)
    db_session.commit()
    db_session.refresh(run)
    return run


# --- TaskType.DETECT -------------------------------------------------------


def test_task_type_detect_is_a_real_enum_member() -> None:
    assert TaskType.DETECT.value == "detect"


def test_task_type_detect_accepted_end_to_end(client: TestClient, db_session) -> None:
    task_run = _build_detect_task_run(client, db_session, uuid.uuid4().hex)
    task = db_session.get(Task, task_run.task_id)
    assert task.task_type == TaskType.DETECT


# --- issue_detection_runs ---------------------------------------------------


def test_minimally_valid_issue_detection_run_is_accepted(
    db_session, detect_task_run: TaskRun
) -> None:
    run = IssueDetectionRun(**_base_run_kwargs(detect_task_run))
    db_session.add(run)
    db_session.commit()  # must NOT raise
    db_session.refresh(run)
    assert run.total_issues_found == 5
    assert run.persisted_issue_count == 5


def test_issue_detection_run_relationship_from_task_run(
    db_session, valid_detection_run: IssueDetectionRun
) -> None:
    db_session.expire_all()
    task_run = db_session.get(TaskRun, valid_detection_run.task_run_id)
    assert task_run.issue_detection_run is not None
    assert task_run.issue_detection_run.id == valid_detection_run.id


def test_second_issue_detection_run_for_same_task_run_is_rejected(
    db_session, valid_detection_run: IssueDetectionRun, detect_task_run: TaskRun
) -> None:
    duplicate = IssueDetectionRun(**_base_run_kwargs(detect_task_run))
    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_negative_rows_scanned_is_rejected(db_session, detect_task_run: TaskRun) -> None:
    kwargs = _base_run_kwargs(detect_task_run)
    kwargs["rows_scanned"] = -1
    db_session.add(IssueDetectionRun(**kwargs))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_negative_columns_scanned_is_rejected(db_session, detect_task_run: TaskRun) -> None:
    kwargs = _base_run_kwargs(detect_task_run)
    kwargs["columns_scanned"] = -1
    db_session.add(IssueDetectionRun(**kwargs))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_negative_total_issues_found_is_rejected(db_session, detect_task_run: TaskRun) -> None:
    kwargs = _base_run_kwargs(detect_task_run)
    kwargs["total_issues_found"] = -1
    db_session.add(IssueDetectionRun(**kwargs))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_persisted_count_exceeding_total_found_is_rejected(
    db_session, detect_task_run: TaskRun
) -> None:
    """Guards the exact invariant that keeps summary totals accurate even
    when detail rows are capped (approved Module 14 correction): the
    persisted count can never exceed the true total."""
    kwargs = _base_run_kwargs(detect_task_run)
    kwargs["total_issues_found"] = 3
    kwargs["persisted_issue_count"] = 5
    db_session.add(IssueDetectionRun(**kwargs))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_persisted_count_less_than_total_is_accepted(
    db_session, detect_task_run: TaskRun
) -> None:
    """The capped case itself must be a legal, expected state, not just
    'not rejected' -- total_issues_found stays the true count."""
    kwargs = _base_run_kwargs(detect_task_run)
    kwargs["total_issues_found"] = 20_000
    kwargs["persisted_issue_count"] = 10_000
    run = IssueDetectionRun(**kwargs)
    db_session.add(run)
    db_session.commit()  # must NOT raise
    db_session.refresh(run)
    assert run.total_issues_found == 20_000
    assert run.persisted_issue_count == 10_000


# --- issues ------------------------------------------------------------


def _base_issue_kwargs(detection_run: IssueDetectionRun) -> dict:
    return dict(
        id=uuid.uuid4(),
        organization_id=detection_run.organization_id,
        detection_run_id=detection_run.id,
        row_number=1,
        column_name="email",
        issue_type="invalid_email",
        severity="MEDIUM",
        original_value="not-an-email",
        suggested_fix=None,
        confidence=1.0,
    )


def test_minimally_valid_issue_is_accepted(
    db_session, valid_detection_run: IssueDetectionRun
) -> None:
    issue = Issue(**_base_issue_kwargs(valid_detection_run))
    db_session.add(issue)
    db_session.commit()  # must NOT raise
    db_session.refresh(issue)
    assert issue.issue_type == "invalid_email"


def test_issue_relationship_from_detection_run(
    db_session, valid_detection_run: IssueDetectionRun
) -> None:
    issue = Issue(**_base_issue_kwargs(valid_detection_run))
    db_session.add(issue)
    db_session.commit()
    db_session.expire_all()
    run = db_session.get(IssueDetectionRun, valid_detection_run.id)
    assert len(run.issues) == 1
    assert run.issues[0].id == issue.id


def test_issue_with_null_column_name_is_accepted(
    db_session, valid_detection_run: IssueDetectionRun
) -> None:
    """duplicate_row is a whole-row finding -- column_name must be
    nullable, not just optional at the API layer."""
    kwargs = _base_issue_kwargs(valid_detection_run)
    kwargs["column_name"] = None
    kwargs["issue_type"] = "duplicate_row"
    kwargs["original_value"] = None
    issue = Issue(**kwargs)
    db_session.add(issue)
    db_session.commit()  # must NOT raise
    db_session.refresh(issue)
    assert issue.column_name is None


def test_issue_with_null_suggested_fix_is_accepted(
    db_session, valid_detection_run: IssueDetectionRun
) -> None:
    """suggested_fix is explicitly optional per the Module 14 contract."""
    kwargs = _base_issue_kwargs(valid_detection_run)
    assert kwargs["suggested_fix"] is None
    issue = Issue(**kwargs)
    db_session.add(issue)
    db_session.commit()  # must NOT raise


def test_invalid_issue_type_is_rejected(
    db_session, valid_detection_run: IssueDetectionRun
) -> None:
    kwargs = _base_issue_kwargs(valid_detection_run)
    kwargs["issue_type"] = "not_a_real_issue_type"
    db_session.add(Issue(**kwargs))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_invalid_severity_is_rejected(db_session, valid_detection_run: IssueDetectionRun) -> None:
    kwargs = _base_issue_kwargs(valid_detection_run)
    kwargs["severity"] = "URGENT"
    db_session.add(Issue(**kwargs))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_lowercase_severity_is_rejected(
    db_session, valid_detection_run: IssueDetectionRun
) -> None:
    """Severity is deliberately uppercase-only (see app.models.enums) --
    'medium' must not silently pass as 'MEDIUM'."""
    kwargs = _base_issue_kwargs(valid_detection_run)
    kwargs["severity"] = "medium"
    db_session.add(Issue(**kwargs))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_negative_row_number_is_rejected(
    db_session, valid_detection_run: IssueDetectionRun
) -> None:
    kwargs = _base_issue_kwargs(valid_detection_run)
    kwargs["row_number"] = -1
    db_session.add(Issue(**kwargs))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.parametrize("confidence", [-0.01, 1.01])
def test_confidence_out_of_range_is_rejected(
    db_session, valid_detection_run: IssueDetectionRun, confidence: float
) -> None:
    kwargs = _base_issue_kwargs(valid_detection_run)
    kwargs["confidence"] = confidence
    db_session.add(Issue(**kwargs))
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


@pytest.mark.parametrize("confidence", [0.0, 1.0, 0.5])
def test_confidence_boundary_values_are_accepted(
    db_session, valid_detection_run: IssueDetectionRun, confidence: float
) -> None:
    kwargs = _base_issue_kwargs(valid_detection_run)
    kwargs["confidence"] = confidence
    kwargs["id"] = uuid.uuid4()
    db_session.add(Issue(**kwargs))
    db_session.commit()  # must NOT raise


def test_every_issue_type_in_the_vocabulary_is_accepted(
    db_session, valid_detection_run: IssueDetectionRun
) -> None:
    """Regression guard against the CHECK constraint and
    app.models.enums.ISSUE_TYPES ever drifting apart -- every one of the
    18 values, including the deferred broken_fk_reference (schema support
    exists even though no rule produces it yet -- see that value's own
    comment in app.models.enums)."""
    assert len(ISSUE_TYPES) == 18
    for issue_type in ISSUE_TYPES:
        kwargs = _base_issue_kwargs(valid_detection_run)
        kwargs["id"] = uuid.uuid4()
        kwargs["issue_type"] = issue_type
        db_session.add(Issue(**kwargs))
        db_session.commit()  # must NOT raise for any of the 18


def test_every_severity_in_the_vocabulary_is_accepted(
    db_session, valid_detection_run: IssueDetectionRun
) -> None:
    assert ISSUE_SEVERITIES == ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")
    for severity in ISSUE_SEVERITIES:
        kwargs = _base_issue_kwargs(valid_detection_run)
        kwargs["id"] = uuid.uuid4()
        kwargs["severity"] = severity
        db_session.add(Issue(**kwargs))
        db_session.commit()  # must NOT raise for any of the 5


def test_issue_cascade_deletes_with_detection_run(
    db_session, valid_detection_run: IssueDetectionRun
) -> None:
    """Verifies the database-level ondelete='CASCADE' on
    fk_issues_org_detection_run directly -- issued as a Core DELETE, not
    db_session.delete(), since the ORM's default relationship handling
    would otherwise try to NULL out issues.organization_id/detection_run_id
    itself before the DB ever sees a DELETE (neither this relationship nor
    CleaningRun.changes' identical, unmodified precedent sets
    passive_deletes=True), which is a Python-side behavior this test is
    not about."""
    from sqlalchemy import delete

    issue = Issue(**_base_issue_kwargs(valid_detection_run))
    db_session.add(issue)
    db_session.commit()
    issue_id = issue.id

    db_session.execute(delete(IssueDetectionRun).where(IssueDetectionRun.id == valid_detection_run.id))
    db_session.commit()

    assert db_session.get(Issue, issue_id) is None


# --- issue_detection_column_rules ------------------------------------------


def _register_org(client: TestClient, suffix: str) -> tuple[dict, str]:
    headers = _auth_headers(client, suffix)
    source_id = client.post(
        "/data-sources",
        json={
            "name": "Rules Source", "source_type": "csv_upload",
            "connection_metadata": {"file_path": "customers.csv"},
        },
        headers=headers,
    ).json()["id"]
    return headers, source_id


def test_minimally_valid_orgwide_column_rule_is_accepted(client: TestClient, db_session) -> None:
    headers = _auth_headers(client, uuid.uuid4().hex)
    org_id = uuid.UUID(
        client.post(
            "/data-sources",
            json={
                "name": "Rules Source", "source_type": "csv_upload",
                "connection_metadata": {"file_path": "x.csv"},
            },
            headers=headers,
        ).json()["organization_id"]
    )
    rule = IssueDetectionColumnRule(
        id=uuid.uuid4(),
        organization_id=org_id,
        data_source_id=None,
        column_name="email",
        expected_type="email",
        is_required=True,
    )
    db_session.add(rule)
    db_session.commit()  # must NOT raise
    db_session.refresh(rule)
    assert rule.is_active is True
    assert rule.outlier_enabled is False
    assert rule.capitalization_check_enabled is False


def test_invalid_expected_type_is_rejected(client: TestClient, db_session) -> None:
    headers, source_id = _register_org(client, uuid.uuid4().hex)
    source = db_session.get(DataSource, uuid.UUID(source_id))
    rule = IssueDetectionColumnRule(
        id=uuid.uuid4(),
        organization_id=source.organization_id,
        data_source_id=source.id,
        column_name="signup_date",
        expected_type="not_a_real_type",
    )
    db_session.add(rule)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_every_expected_type_in_the_vocabulary_is_accepted(
    client: TestClient, db_session
) -> None:
    assert ISSUE_DETECTION_EXPECTED_TYPES == ("email", "phone", "date", "numeric", "boolean")
    headers, source_id = _register_org(client, uuid.uuid4().hex)
    source = db_session.get(DataSource, uuid.UUID(source_id))
    for i, expected_type in enumerate(ISSUE_DETECTION_EXPECTED_TYPES):
        rule = IssueDetectionColumnRule(
            id=uuid.uuid4(),
            organization_id=source.organization_id,
            data_source_id=source.id,
            column_name=f"col_{i}",
            expected_type=expected_type,
        )
        db_session.add(rule)
        db_session.commit()  # must NOT raise for any of the 5


def test_negative_outlier_threshold_is_rejected(client: TestClient, db_session) -> None:
    headers, source_id = _register_org(client, uuid.uuid4().hex)
    source = db_session.get(DataSource, uuid.UUID(source_id))
    rule = IssueDetectionColumnRule(
        id=uuid.uuid4(),
        organization_id=source.organization_id,
        data_source_id=source.id,
        column_name="amount",
        outlier_enabled=True,
        outlier_zscore_threshold=-1.0,
    )
    db_session.add(rule)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_zero_outlier_threshold_is_rejected(client: TestClient, db_session) -> None:
    headers, source_id = _register_org(client, uuid.uuid4().hex)
    source = db_session.get(DataSource, uuid.UUID(source_id))
    rule = IssueDetectionColumnRule(
        id=uuid.uuid4(),
        organization_id=source.organization_id,
        data_source_id=source.id,
        column_name="amount",
        outlier_enabled=True,
        outlier_zscore_threshold=0.0,
    )
    db_session.add(rule)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_null_outlier_threshold_is_accepted(client: TestClient, db_session) -> None:
    """NULL means 'fall back to the global setting' -- must not be
    conflated with an invalid explicit value."""
    headers, source_id = _register_org(client, uuid.uuid4().hex)
    source = db_session.get(DataSource, uuid.UUID(source_id))
    rule = IssueDetectionColumnRule(
        id=uuid.uuid4(),
        organization_id=source.organization_id,
        data_source_id=source.id,
        column_name="amount",
        outlier_enabled=True,
        outlier_zscore_threshold=None,
    )
    db_session.add(rule)
    db_session.commit()  # must NOT raise


def test_duplicate_active_orgwide_rule_for_same_column_is_rejected(
    client: TestClient, db_session
) -> None:
    headers, source_id = _register_org(client, uuid.uuid4().hex)
    source = db_session.get(DataSource, uuid.UUID(source_id))
    db_session.add(
        IssueDetectionColumnRule(
            id=uuid.uuid4(), organization_id=source.organization_id,
            data_source_id=None, column_name="email", expected_type="email",
        )
    )
    db_session.commit()

    db_session.add(
        IssueDetectionColumnRule(
            id=uuid.uuid4(), organization_id=source.organization_id,
            data_source_id=None, column_name="EMAIL  ", expected_type="email",
        )
    )
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_datasource_scoped_and_orgwide_rules_for_same_column_can_coexist(
    client: TestClient, db_session
) -> None:
    """The two-partial-index design's whole point: a data-source-specific
    override and an org-wide default for the same column name are not the
    same uniqueness key."""
    headers, source_id = _register_org(client, uuid.uuid4().hex)
    source = db_session.get(DataSource, uuid.UUID(source_id))
    db_session.add(
        IssueDetectionColumnRule(
            id=uuid.uuid4(), organization_id=source.organization_id,
            data_source_id=None, column_name="phone", expected_type="phone",
        )
    )
    db_session.add(
        IssueDetectionColumnRule(
            id=uuid.uuid4(), organization_id=source.organization_id,
            data_source_id=source.id, column_name="phone", expected_type="phone",
        )
    )
    db_session.commit()  # must NOT raise


def test_inactive_duplicate_column_rule_does_not_block_new_active_one(
    client: TestClient, db_session
) -> None:
    """Soft-deleting (is_active=False) frees the column_name for a new
    active rule -- same convention DataSource/Task names already use."""
    headers, source_id = _register_org(client, uuid.uuid4().hex)
    source = db_session.get(DataSource, uuid.UUID(source_id))
    db_session.add(
        IssueDetectionColumnRule(
            id=uuid.uuid4(), organization_id=source.organization_id,
            data_source_id=None, column_name="email", is_active=False,
        )
    )
    db_session.commit()

    db_session.add(
        IssueDetectionColumnRule(
            id=uuid.uuid4(), organization_id=source.organization_id,
            data_source_id=None, column_name="email", is_active=True,
        )
    )
    db_session.commit()  # must NOT raise
