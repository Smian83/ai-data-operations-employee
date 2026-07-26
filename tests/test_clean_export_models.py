"""Tests for the CleanExport ORM model (Module 19 Phase 1).

Covers:
  - Table creation (implicit via create_all in conftest)
  - Valid INSERT and SELECT
  - CHECK constraint enforcement (status, format, row_count, column_count)
  - UNIQUE constraint on idempotency_key
  - NULL fields for non-completed rows
  - Completed row: all fields populated
"""
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.clean_export import CleanExport
from app.models.enums import CLEAN_EXPORT_FORMATS, CLEAN_EXPORT_STATUSES, TaskType


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_org(db_session):
    from app.models.organization import Organization
    slug = f"ce-test-org-{uuid.uuid4().hex[:6]}"
    org = Organization(id=uuid.uuid4(), name=f"CE Test Org {slug}", slug=slug)
    db_session.add(org)
    db_session.commit()
    return org


def _make_data_source(db_session, org_id):
    from app.models.data_source import DataSource
    ds = DataSource(
        id=uuid.uuid4(),
        organization_id=org_id,
        name=f"ds-{uuid.uuid4().hex[:6]}",
        source_type="csv_upload",
        connection_metadata={"file_path": "test.csv"},
        is_active=True,
    )
    db_session.add(ds)
    db_session.commit()
    return ds


def _make_task(db_session, org_id, ds_id):
    from app.models.task import Task
    task = Task(
        id=uuid.uuid4(),
        organization_id=org_id,
        data_source_id=ds_id,
        name=f"export-task-{uuid.uuid4().hex[:6]}",
        task_type=TaskType.EXPORT,
        is_active=True,
    )
    db_session.add(task)
    db_session.commit()
    return task


def _make_clean_export(
    db_session,
    org_id,
    task_id,
    ds_id,
    *,
    status: str = "completed",
    format: str = "csv",
    idempotency_key: str | None = None,
    artifact_id: uuid.UUID | None = None,
    checksum: str | None = None,
    row_count: int | None = None,
    column_count: int | None = None,
    failure_reason: str | None = None,
    dataset_version: uuid.UUID | None = None,
) -> CleanExport:
    if idempotency_key is None:
        idempotency_key = str(uuid.uuid4())
    if status == "completed" and artifact_id is None:
        artifact_id = uuid.uuid4()
    if status == "completed" and checksum is None:
        checksum = "a" * 64
    if status == "completed" and row_count is None:
        row_count = 10
    if status == "completed" and column_count is None:
        column_count = 3
    now = datetime.now(timezone.utc)
    ce = CleanExport(
        id=uuid.uuid4(),
        organization_id=org_id,
        job_id=task_id,
        data_source_id=ds_id,
        dataset_version=dataset_version,
        format=format,
        status=status,
        artifact_id=artifact_id,
        checksum=checksum,
        row_count=row_count,
        column_count=column_count,
        idempotency_key=idempotency_key,
        failure_reason=failure_reason,
        completed_at=now if status in ("completed", "failed", "blocked") else None,
    )
    db_session.add(ce)
    db_session.commit()
    db_session.refresh(ce)
    return ce


# ---------------------------------------------------------------------------
# Fixture
# ---------------------------------------------------------------------------

@pytest.fixture
def entities(db_session):
    org = _make_org(db_session)
    ds = _make_data_source(db_session, org.id)
    task = _make_task(db_session, org.id, ds.id)
    return org, ds, task


# ---------------------------------------------------------------------------
# Basic creation tests
# ---------------------------------------------------------------------------

class TestCleanExportCreation:

    def test_completed_row_all_fields(self, db_session, entities):
        org, ds, task = entities
        artifact_id = uuid.uuid4()
        qc_run_id = uuid.uuid4()
        ce = _make_clean_export(
            db_session, org.id, task.id, ds.id,
            status="completed",
            format="csv",
            artifact_id=artifact_id,
            checksum="b" * 64,
            row_count=100,
            column_count=5,
            dataset_version=qc_run_id,
        )
        assert ce.id is not None
        assert ce.organization_id == org.id
        assert ce.job_id == task.id
        assert ce.data_source_id == ds.id
        assert ce.format == "csv"
        assert ce.status == "completed"
        assert ce.artifact_id == artifact_id
        assert ce.checksum == "b" * 64
        assert ce.row_count == 100
        assert ce.column_count == 5
        assert ce.dataset_version == qc_run_id
        assert ce.failure_reason is None
        assert ce.created_at is not None
        assert ce.completed_at is not None

    def test_blocked_row_nullable_fields_null(self, db_session, entities):
        org, ds, task = entities
        ce = _make_clean_export(
            db_session, org.id, task.id, ds.id,
            status="blocked",
            format="csv",
            artifact_id=None,
            checksum=None,
            row_count=None,
            column_count=None,
            failure_reason="No approved ExportRun found",
        )
        assert ce.status == "blocked"
        assert ce.artifact_id is None
        assert ce.checksum is None
        assert ce.row_count is None
        assert ce.column_count is None
        assert ce.failure_reason == "No approved ExportRun found"
        assert ce.completed_at is not None

    def test_failed_row(self, db_session, entities):
        org, ds, task = entities
        ce = _make_clean_export(
            db_session, org.id, task.id, ds.id,
            status="failed",
            format="xlsx",
            failure_reason="io_error writing artifact: disk full",
            artifact_id=None,
            checksum=None,
            row_count=None,
            column_count=None,
        )
        assert ce.status == "failed"
        assert ce.format == "xlsx"
        assert ce.failure_reason.startswith("io_error")

    def test_dataset_version_nullable(self, db_session, entities):
        org, ds, task = entities
        ce = _make_clean_export(
            db_session, org.id, task.id, ds.id,
            status="blocked",
            dataset_version=None,
            artifact_id=None,
            checksum=None,
            row_count=None,
            column_count=None,
        )
        assert ce.dataset_version is None

    def test_all_statuses_accepted(self, db_session, entities):
        org, ds, task = entities
        for s in CLEAN_EXPORT_STATUSES:
            ce = _make_clean_export(
                db_session, org.id, task.id, ds.id,
                status=s,
                artifact_id=None,
                checksum=None,
                row_count=None,
                column_count=None,
                failure_reason="test" if s in ("failed", "blocked", "expired") else None,
            )
            assert ce.status == s

    def test_all_formats_accepted(self, db_session, entities):
        org, ds, task = entities
        for fmt in CLEAN_EXPORT_FORMATS:
            ce = _make_clean_export(
                db_session, org.id, task.id, ds.id,
                format=fmt,
            )
            assert ce.format == fmt


# ---------------------------------------------------------------------------
# UNIQUE constraint: idempotency_key
# ---------------------------------------------------------------------------

class TestCleanExportUniqueConstraint:

    def test_duplicate_idempotency_key_raises(self, db_session, entities):
        org, ds, task = entities
        key = str(uuid.uuid4())
        _make_clean_export(
            db_session, org.id, task.id, ds.id,
            idempotency_key=key,
        )
        with pytest.raises(IntegrityError):
            _make_clean_export(
                db_session, org.id, task.id, ds.id,
                idempotency_key=key,
            )

    def test_different_idempotency_keys_allowed(self, db_session, entities):
        org, ds, task = entities
        ce1 = _make_clean_export(
            db_session, org.id, task.id, ds.id,
            idempotency_key=str(uuid.uuid4()),
        )
        ce2 = _make_clean_export(
            db_session, org.id, task.id, ds.id,
            idempotency_key=str(uuid.uuid4()),
        )
        assert ce1.id != ce2.id


# ---------------------------------------------------------------------------
# CHECK constraints
# ---------------------------------------------------------------------------

class TestCleanExportCheckConstraints:

    def test_invalid_status_raises(self, db_session, entities):
        org, ds, task = entities
        with pytest.raises(IntegrityError):
            _make_clean_export(
                db_session, org.id, task.id, ds.id,
                status="INVALID_STATUS",
            )

    def test_invalid_format_raises(self, db_session, entities):
        org, ds, task = entities
        with pytest.raises(IntegrityError):
            _make_clean_export(
                db_session, org.id, task.id, ds.id,
                format="json",
            )

    def test_negative_row_count_raises(self, db_session, entities):
        org, ds, task = entities
        with pytest.raises(IntegrityError):
            ce = CleanExport(
                id=uuid.uuid4(),
                organization_id=org.id,
                job_id=task.id,
                data_source_id=ds.id,
                format="csv",
                status="completed",
                artifact_id=uuid.uuid4(),
                checksum="c" * 64,
                row_count=-1,  # invalid
                column_count=3,
                idempotency_key=str(uuid.uuid4()),
            )
            db_session.add(ce)
            db_session.commit()

    def test_zero_column_count_raises(self, db_session, entities):
        org, ds, task = entities
        with pytest.raises(IntegrityError):
            ce = CleanExport(
                id=uuid.uuid4(),
                organization_id=org.id,
                job_id=task.id,
                data_source_id=ds.id,
                format="csv",
                status="completed",
                artifact_id=uuid.uuid4(),
                checksum="d" * 64,
                row_count=10,
                column_count=0,  # invalid -- must be >= 1
                idempotency_key=str(uuid.uuid4()),
            )
            db_session.add(ce)
            db_session.commit()

    def test_zero_row_count_allowed(self, db_session, entities):
        """Zero rows is a valid (empty dataset) clean export."""
        org, ds, task = entities
        ce = _make_clean_export(
            db_session, org.id, task.id, ds.id,
            status="completed",
            row_count=0,
            column_count=3,
        )
        assert ce.row_count == 0

    def test_null_row_count_allowed(self, db_session, entities):
        """NULL row_count is valid for non-completed rows."""
        org, ds, task = entities
        ce = _make_clean_export(
            db_session, org.id, task.id, ds.id,
            status="blocked",
            row_count=None,
            column_count=None,
            artifact_id=None,
            checksum=None,
        )
        assert ce.row_count is None

    def test_null_column_count_allowed(self, db_session, entities):
        org, ds, task = entities
        ce = _make_clean_export(
            db_session, org.id, task.id, ds.id,
            status="failed",
            row_count=None,
            column_count=None,
            artifact_id=None,
            checksum=None,
        )
        assert ce.column_count is None


# ---------------------------------------------------------------------------
# Repr
# ---------------------------------------------------------------------------

class TestCleanExportRepr:

    def test_repr_contains_key_fields(self, db_session, entities):
        org, ds, task = entities
        ce = _make_clean_export(db_session, org.id, task.id, ds.id)
        r = repr(ce)
        assert "CleanExport" in r
        assert "completed" in r
        assert "csv" in r
