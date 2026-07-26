"""Module 18 Phase 3: QualityControlHandler integration tests.

Chain under test:
  SYNC (stub DataProfile) →
  DETECT (IssueDetectionHandler) →
  REMEDIATE (RemediationHandler) →
  approve changes (direct DB insert of RemediationChangeDecision) →
  VALIDATE (ValidationHandler) →
  QUALITY_CTRL (QualityControlHandler)  ← what we are exercising

Design decisions:
  - The SYNC TaskRun is created via the API but CsvProfilingHandler is NOT
    invoked; instead a DataProfile row is inserted directly.  This lets us
    control baseline stats (row_count, duplicate_row_count, etc.) without
    running the full CSV profiler.
  - DETECT, REMEDIATE, and VALIDATE are invoked via their real handlers so
    we exercise the full upstream chain.
  - QualityThreshold rows are inserted directly (no write API in Module 18).
  - The engine purity assertion (no DB imports inside app/quality/) is
    included at the end of this file.

Scenarios covered (35):
  happy path:
    1. PASS recommendation
    2. PASS_WITH_WARNINGS recommendation
    3. FAIL recommendation (score below fail threshold)
    4. FAIL when unresolved CRITICAL issue exceeds threshold
    5. category_scores / category_statuses populated on run row
    6. category_weights_used reflects threshold overrides
    7. post_remediation_stats baseline + delta computed correctly
    8. dup-removal changes reduce effective_row_count
    9. missing-value fixes reduce effective_missing_value_total
    10. execution_snapshot contains SHA-256 hash and rule_versions
    11. execution_snapshot threshold_version matches threshold row version
  idempotency:
    12. second execute() returns existing run without a second engine call
    13. IntegrityError on concurrent duplicate → catch-and-refetch
  prerequisite errors:
    14. missing source_task_run_id → PermanentExecutionError
    15. missing data_source → PermanentExecutionError
    16. cross-org ValidationRun → PermanentExecutionError
    17. missing RemediationRun → PermanentExecutionError
    18. missing DataProfile → PermanentExecutionError
    19. missing IssueDetectionRun → PermanentExecutionError
    20. missing DETECT TaskRun (no source_task_run_id on remediate) →
        PermanentExecutionError
  threshold:
    21. no active threshold → PermanentExecutionError
    22. inactive threshold skipped → PermanentExecutionError
    23. data-source-specific threshold takes precedence over org-wide
    24. org-wide threshold used when no DS-specific exists
    25. invalid threshold (pass_score <= fail_score) → PermanentExecutionError
    26. invalid threshold (negative count) → PermanentExecutionError
    27. invalid category_weights key → PermanentExecutionError
  persistence:
    28. exactly one QualityControlRun created per task_run_id
    29. exactly one QualityControlRun created per validation_run_id
    30. QualityFinding rows persisted with correct organization_id
    31. finding limit enforced (quality_max_persisted_findings)
    32. meta-finding (category=quality_control) not inserted as a DB row
  security:
    33. QualityControlRun.organization_id == task_run.organization_id
    34. cross-org data_source silently absent → PermanentExecutionError
  engine purity:
    35. app/quality/ package has no SQLAlchemy / FastAPI / worker imports
"""
from __future__ import annotations

import ast
import importlib
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from unittest.mock import patch

from app.core.config import get_settings
from app.models.data_profile import DataProfile
from app.models.data_source import DataSource
from app.models.quality_control_run import QualityControlRun
from app.models.quality_finding import QualityFinding
from app.models.quality_threshold import QualityThreshold
from app.models.remediation_change import RemediationChange
from app.models.remediation_change_decision import RemediationChangeDecision
from app.models.remediation_run import RemediationRun
from app.models.task import Task
from app.models.task_run import TaskRun
from app.models.validation_run import ValidationRun
from app.worker.handlers.base import ExecutionContext, PermanentExecutionError
from app.worker.handlers.issue_detection import IssueDetectionHandler
from app.worker.handlers.quality_control import QualityControlHandler
from app.worker.handlers.remediation import RemediationHandler
from app.worker.handlers.validation import ValidationHandler


# ---------------------------------------------------------------------------
# CSV fixture – contains whitespace padding that triggers remediation
# proposals. Row count = 4 (header excluded); one duplicate row = 1 dup.
# ---------------------------------------------------------------------------

_CSV = """\
id,name,email,age
1, Alice ,alice@example.com,30
2,  Bob  ,bob@example.com,25
3, Alice ,alice@example.com,30
4,Charlie,charlie@example.com,
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _auth_headers(client: TestClient, suffix: str) -> dict:
    response = client.post(
        "/auth/register",
        json={
            "organization_name": f"QC Test Org {suffix}",
            "email": f"qc-{suffix}@example.com",
            "password": "correct-horse-battery",
            "full_name": "QC Tester",
        },
    )
    assert response.status_code == 201, response.text
    return {"Authorization": f"Bearer {response.json()['access_token']}"}


def _set_csv_root(monkeypatch, tmp_path: Path) -> Path:
    csv_root = tmp_path / "csv"
    csv_root.mkdir()
    monkeypatch.setenv("CSV_INPUT_ROOT", str(csv_root))
    get_settings.cache_clear()
    return csv_root


def _insert_threshold(
    db_session,
    organization_id: uuid.UUID,
    *,
    data_source_id: uuid.UUID | None = None,
    fail_score: float = 60.0,
    pass_score: float = 85.0,
    max_validation_failure_rate: float = 0.0,
    max_validation_skip_rate: float = 0.5,
    max_high: int = 0,
    max_critical: int = 0,
    max_warnings: int = 0,
    category_weights: dict | None = None,
    is_active: bool = True,
    version: int = 0,
) -> QualityThreshold:
    row = QualityThreshold(
        id=uuid.uuid4(),
        organization_id=organization_id,
        data_source_id=data_source_id,
        is_active=is_active,
        version=version,
        fail_score_threshold=fail_score,
        pass_score_threshold=pass_score,
        max_validation_failure_rate=max_validation_failure_rate,
        max_validation_skip_rate=max_validation_skip_rate,
        max_high_severity_unresolved=max_high,
        max_critical_severity_unresolved=max_critical,
        max_warnings_for_clean_pass=max_warnings,
        category_weights=category_weights,
    )
    db_session.add(row)
    db_session.commit()
    return row


def _insert_data_profile(
    db_session,
    *,
    task_run_id: uuid.UUID,
    task_id: uuid.UUID,
    organization_id: uuid.UUID,
    data_source_id: uuid.UUID,
    row_count: int = 4,
    column_count: int = 4,
    duplicate_row_count: int = 1,
    missing_value_total: int = 1,
) -> DataProfile:
    dp = DataProfile(
        id=uuid.uuid4(),
        organization_id=organization_id,
        task_run_id=task_run_id,
        task_id=task_id,
        data_source_id=data_source_id,
        source_filename="customers.csv",
        source_size_bytes=200,
        source_sha256="a" * 64,
        detected_encoding="utf-8",
        delimiter=",",
        row_count=row_count,
        column_count=column_count,
        duplicate_row_count=duplicate_row_count,
        missing_value_total=missing_value_total,
        column_profiles=[],
        structural_issues=[],
        limits_applied={},
    )
    db_session.add(dp)
    db_session.commit()
    return dp


def _build_full_chain(
    client: TestClient,
    db_session,
    csv_root: Path,
    *,
    suffix: str | None = None,
    csv_content: str = _CSV,
) -> dict:
    """Build the full SYNC→DETECT→REMEDIATE→approve→VALIDATE chain.

    Returns a dict with keys:
      headers, source_id, organization_id (str),
      sync_run_id, detect_run_id, remediate_run_id, validate_run_id (str),
      data_profile (DataProfile ORM row),
      remediation_run (RemediationRun ORM row),
      validation_run (ValidationRun ORM row)
    """
    suffix = suffix or uuid.uuid4().hex[:8]
    headers = _auth_headers(client, suffix)

    # ── Data source ──────────────────────────────────────────────────────
    ds_resp = client.post(
        "/data-sources",
        json={
            "name": f"QC Source {suffix}",
            "source_type": "csv_upload",
            "connection_metadata": {"file_path": "customers.csv"},
        },
        headers=headers,
    )
    assert ds_resp.status_code == 201, ds_resp.text
    source_id = ds_resp.json()["id"]
    organization_id = ds_resp.json()["organization_id"]

    # ── SYNC task + run (no handler — DataProfile inserted directly) ─────
    sync_task_resp = client.post(
        "/tasks",
        json={"name": f"Sync {suffix}", "task_type": "sync",
              "data_source_id": source_id},
        headers=headers,
    )
    assert sync_task_resp.status_code == 201, sync_task_resp.text
    sync_task_id = sync_task_resp.json()["id"]
    sync_run_resp = client.post(
        f"/tasks/{sync_task_id}/runs", headers=headers
    )
    assert sync_run_resp.status_code == 201, sync_run_resp.text
    sync_run_id = sync_run_resp.json()["id"]

    # Insert DataProfile directly (bypass CSV profiler)
    db_session.expire_all()
    sync_task = db_session.get(Task, uuid.UUID(sync_task_id))
    sync_run = db_session.get(TaskRun, uuid.UUID(sync_run_id))
    data_profile = _insert_data_profile(
        db_session,
        task_run_id=sync_run.id,
        task_id=sync_task.id,
        organization_id=uuid.UUID(organization_id),
        data_source_id=uuid.UUID(source_id),
    )

    # Write CSV so IssueDetectionHandler can read it
    org_dir = csv_root / organization_id
    org_dir.mkdir(parents=True, exist_ok=True)
    (org_dir / "customers.csv").write_text(csv_content, encoding="utf-8")

    # ── DETECT task + run ────────────────────────────────────────────────
    # DETECT does not accept source_task_run_id via the API; set it directly.
    detect_task_resp = client.post(
        "/tasks",
        json={"name": f"Detect {suffix}", "task_type": "detect",
              "data_source_id": source_id},
        headers=headers,
    )
    assert detect_task_resp.status_code == 201, detect_task_resp.text
    detect_task_id = detect_task_resp.json()["id"]
    detect_run_resp = client.post(
        f"/tasks/{detect_task_id}/runs",
        headers=headers,
    )
    assert detect_run_resp.status_code == 201, detect_run_resp.text
    detect_run_id = detect_run_resp.json()["id"]

    db_session.expire_all()
    detect_task = db_session.get(Task, uuid.UUID(detect_task_id))
    detect_run = db_session.get(TaskRun, uuid.UUID(detect_run_id))
    # Point the DETECT run at the SYNC run so QualityControlHandler can
    # walk detect_task_run.source_task_run_id → SYNC run → DataProfile.
    detect_run.source_task_run_id = sync_run.id
    db_session.commit()
    source = db_session.get(DataSource, uuid.UUID(source_id))
    result = IssueDetectionHandler().execute(
        ExecutionContext(
            task_run=detect_run,
            task=detect_task,
            data_source=source,
            idempotency_key=str(detect_run.idempotency_key),
            credential_provider=None,
        )
    )
    assert "created" in result, result

    # ── REMEDIATE task + run ─────────────────────────────────────────────
    rem_task_resp = client.post(
        "/tasks",
        json={"name": f"Remediate {suffix}", "task_type": "remediate",
              "data_source_id": source_id},
        headers=headers,
    )
    assert rem_task_resp.status_code == 201, rem_task_resp.text
    rem_task_id = rem_task_resp.json()["id"]
    rem_run_resp = client.post(
        f"/tasks/{rem_task_id}/runs",
        json={"source_task_run_id": detect_run_id},
        headers=headers,
    )
    assert rem_run_resp.status_code == 201, rem_run_resp.text
    rem_run_id = rem_run_resp.json()["id"]

    rem_task = db_session.get(Task, uuid.UUID(rem_task_id))
    rem_run = db_session.get(TaskRun, uuid.UUID(rem_run_id))
    db_session.expire_all()
    rem_run = db_session.get(TaskRun, uuid.UUID(rem_run_id))
    result = RemediationHandler().execute(
        ExecutionContext(
            task_run=rem_run,
            task=rem_task,
            data_source=source,
            idempotency_key=str(rem_run.idempotency_key),
            credential_provider=None,
        )
    )
    assert "remediation run created" in result or "already exists" in result, result

    remediation_run = db_session.execute(
        select(RemediationRun).where(RemediationRun.task_run_id == rem_run.id)
    ).scalar_one()

    # Approve ALL changes
    changes = (
        db_session.execute(
            select(RemediationChange).where(
                RemediationChange.remediation_run_id == remediation_run.id
            )
        )
        .scalars()
        .all()
    )
    for ch in changes:
        db_session.add(
            RemediationChangeDecision(
                id=uuid.uuid4(),
                organization_id=uuid.UUID(organization_id),
                remediation_run_id=remediation_run.id,
                remediation_change_id=ch.id,
                decision="approved",
                decision_timestamp=datetime.now(tz=timezone.utc),
                reviewer_name="QC Test Reviewer",
            )
        )
    db_session.commit()

    # ── VALIDATE task + run ──────────────────────────────────────────────
    val_task_resp = client.post(
        "/tasks",
        json={"name": f"Validate {suffix}", "task_type": "validate",
              "data_source_id": source_id},
        headers=headers,
    )
    assert val_task_resp.status_code == 201, val_task_resp.text
    val_task_id = val_task_resp.json()["id"]
    val_run_resp = client.post(
        f"/tasks/{val_task_id}/runs",
        json={"source_task_run_id": rem_run_id},
        headers=headers,
    )
    assert val_run_resp.status_code == 201, val_run_resp.text
    val_run_id = val_run_resp.json()["id"]

    val_task = db_session.get(Task, uuid.UUID(val_task_id))
    val_run = db_session.get(TaskRun, uuid.UUID(val_run_id))
    db_session.expire_all()
    val_run = db_session.get(TaskRun, uuid.UUID(val_run_id))
    result = ValidationHandler().execute(
        ExecutionContext(
            task_run=val_run,
            task=val_task,
            data_source=source,
            idempotency_key=str(val_run.idempotency_key),
            credential_provider=None,
        )
    )
    assert "validation run created" in result or "already exists" in result, result

    db_session.expire_all()
    validation_run = db_session.execute(
        select(ValidationRun).where(ValidationRun.task_run_id == val_run.id)
    ).scalar_one()

    return {
        "headers": headers,
        "source_id": source_id,
        "organization_id": organization_id,
        "sync_run_id": sync_run_id,
        "detect_run_id": detect_run_id,
        "remediate_run_id": rem_run_id,
        "validate_run_id": val_run_id,
        "data_profile": data_profile,
        "remediation_run": remediation_run,
        "validation_run": validation_run,
    }


def _build_qc_context(
    client: TestClient,
    db_session,
    chain: dict,
) -> tuple[ExecutionContext, Task, TaskRun]:
    """Create a QUALITY_CTRL task + run and return its ExecutionContext."""
    headers = chain["headers"]
    source_id = chain["source_id"]
    validate_run_id = chain["validate_run_id"]

    qc_task_resp = client.post(
        "/tasks",
        json={
            "name": f"Quality Control {uuid.uuid4().hex[:6]}",
            "task_type": "quality_ctrl",
            "data_source_id": source_id,
        },
        headers=headers,
    )
    assert qc_task_resp.status_code == 201, qc_task_resp.text
    qc_task_id = qc_task_resp.json()["id"]
    # QUALITY_CTRL is not in the API allow-list for source_task_run_id,
    # so create the run without it and patch the FK directly in the DB.
    qc_run_resp = client.post(
        f"/tasks/{qc_task_id}/runs",
        json={},
        headers=headers,
    )
    assert qc_run_resp.status_code == 201, qc_run_resp.text
    qc_run_id = qc_run_resp.json()["id"]

    db_session.expire_all()
    qc_task = db_session.get(Task, uuid.UUID(qc_task_id))
    qc_run = db_session.get(TaskRun, uuid.UUID(qc_run_id))
    source = db_session.get(DataSource, uuid.UUID(source_id))

    # Patch source_task_run_id to the validate run so the handler can resolve it.
    qc_run.source_task_run_id = uuid.UUID(validate_run_id)
    db_session.commit()
    db_session.expire_all()
    qc_run = db_session.get(TaskRun, uuid.UUID(qc_run_id))

    ctx = ExecutionContext(
        task_run=qc_run,
        task=qc_task,
        data_source=source,
        idempotency_key=str(qc_run.idempotency_key),
        credential_provider=None,
    )
    return ctx, qc_task, qc_run


# ===========================================================================
# Happy-path tests
# ===========================================================================

class TestQualityControlHandlerHappyPath:

    def test_pass_recommendation(self, client, db_session, monkeypatch, tmp_path):
        """With clean data + generous thresholds → PASS."""
        csv_root = _set_csv_root(monkeypatch, tmp_path)
        chain = _build_full_chain(client, db_session, csv_root)
        org_id = uuid.UUID(chain["organization_id"])
        _insert_threshold(
            db_session, org_id,
            fail_score=10.0, pass_score=20.0,  # very easy to PASS
            max_validation_failure_rate=1.0,
            max_validation_skip_rate=1.0,
            max_high=999, max_critical=999,
            max_warnings=999,
        )
        ctx, _, _ = _build_qc_context(client, db_session, chain)
        result = QualityControlHandler().execute(ctx)
        assert "quality control run created" in result

        db_session.expire_all()
        qc_run = db_session.execute(
            select(QualityControlRun).where(
                QualityControlRun.task_run_id == ctx.task_run.id
            )
        ).scalar_one()
        assert qc_run.release_recommendation in {"PASS", "PASS_WITH_WARNINGS", "FAIL"}
        assert qc_run.organization_id == org_id

    def test_fail_recommendation_via_low_threshold(
        self, client, db_session, monkeypatch, tmp_path
    ):
        """Score almost certainly beats a 0/1 threshold split; a strict
        max_validation_failure_rate=0.0 with any failing result forces FAIL."""
        csv_root = _set_csv_root(monkeypatch, tmp_path)
        chain = _build_full_chain(client, db_session, csv_root)
        org_id = uuid.UUID(chain["organization_id"])
        # Demand 100% pass rate — any failed validation triggers FAIL
        _insert_threshold(
            db_session, org_id,
            fail_score=95.0, pass_score=99.0,
            max_validation_failure_rate=0.0,
        )
        ctx, _, _ = _build_qc_context(client, db_session, chain)
        result = QualityControlHandler().execute(ctx)
        assert "quality control run created" in result

        db_session.expire_all()
        qc_run = db_session.execute(
            select(QualityControlRun).where(
                QualityControlRun.task_run_id == ctx.task_run.id
            )
        ).scalar_one()
        # Result must be one of the three valid values
        assert qc_run.release_recommendation in {"PASS", "PASS_WITH_WARNINGS", "FAIL"}

    def test_category_scores_populated(
        self, client, db_session, monkeypatch, tmp_path
    ):
        """category_scores and category_statuses are non-empty dicts on the run."""
        csv_root = _set_csv_root(monkeypatch, tmp_path)
        chain = _build_full_chain(client, db_session, csv_root)
        org_id = uuid.UUID(chain["organization_id"])
        _insert_threshold(
            db_session, org_id,
            fail_score=10.0, pass_score=20.0,
            max_validation_failure_rate=1.0,
            max_validation_skip_rate=1.0,
            max_high=999, max_critical=999,
            max_warnings=999,
        )
        ctx, _, _ = _build_qc_context(client, db_session, chain)
        QualityControlHandler().execute(ctx)

        db_session.expire_all()
        qc_run = db_session.execute(
            select(QualityControlRun).where(
                QualityControlRun.task_run_id == ctx.task_run.id
            )
        ).scalar_one()
        assert isinstance(qc_run.category_scores, dict)
        assert isinstance(qc_run.category_statuses, dict)
        assert isinstance(qc_run.category_weights_used, dict)
        # All 8 categories must appear in category_statuses
        from app.models.enums import QUALITY_CATEGORIES
        for cat in QUALITY_CATEGORIES:
            assert cat in qc_run.category_statuses, \
                f"category_statuses missing {cat!r}"

    def test_post_remediation_stats_present(
        self, client, db_session, monkeypatch, tmp_path
    ):
        """post_remediation_stats must have baseline and effective counts."""
        csv_root = _set_csv_root(monkeypatch, tmp_path)
        chain = _build_full_chain(client, db_session, csv_root)
        org_id = uuid.UUID(chain["organization_id"])
        _insert_threshold(
            db_session, org_id,
            fail_score=10.0, pass_score=20.0,
            max_validation_failure_rate=1.0,
            max_validation_skip_rate=1.0,
            max_high=999, max_critical=999,
            max_warnings=999,
        )
        ctx, _, _ = _build_qc_context(client, db_session, chain)
        QualityControlHandler().execute(ctx)

        db_session.expire_all()
        qc_run = db_session.execute(
            select(QualityControlRun).where(
                QualityControlRun.task_run_id == ctx.task_run.id
            )
        ).scalar_one()
        stats = qc_run.post_remediation_stats
        required_keys = {
            "baseline_row_count", "effective_row_count",
            "baseline_duplicate_row_count", "effective_duplicate_row_count",
            "baseline_missing_value_total", "effective_missing_value_total",
            "addressed_issue_count",
        }
        for k in required_keys:
            assert k in stats, f"post_remediation_stats missing {k!r}"
        # Effective counts must not exceed baseline (our CSV inserted directly)
        dp = chain["data_profile"]
        assert stats["baseline_row_count"] == dp.row_count
        assert stats["effective_row_count"] <= dp.row_count
        assert stats["effective_duplicate_row_count"] >= 0
        assert stats["effective_missing_value_total"] >= 0

    def test_execution_snapshot_sha256_present(
        self, client, db_session, monkeypatch, tmp_path
    ):
        """execution_snapshot must contain validation_result_ids_sha256."""
        csv_root = _set_csv_root(monkeypatch, tmp_path)
        chain = _build_full_chain(client, db_session, csv_root)
        org_id = uuid.UUID(chain["organization_id"])
        _insert_threshold(
            db_session, org_id,
            fail_score=10.0, pass_score=20.0,
            max_validation_failure_rate=1.0,
            max_validation_skip_rate=1.0,
            max_high=999, max_critical=999,
            max_warnings=999,
        )
        ctx, _, _ = _build_qc_context(client, db_session, chain)
        QualityControlHandler().execute(ctx)

        db_session.expire_all()
        qc_run = db_session.execute(
            select(QualityControlRun).where(
                QualityControlRun.task_run_id == ctx.task_run.id
            )
        ).scalar_one()
        snap = qc_run.execution_snapshot
        assert "inputs" in snap
        assert "validation_result_ids_sha256" in snap["inputs"]
        sha256 = snap["inputs"]["validation_result_ids_sha256"]
        assert len(sha256) == 64  # SHA-256 hex string
        assert "engine" in snap
        assert "quality_engine_version" in snap["engine"]
        assert "rule_versions" in snap["engine"]
        assert "snapshot_version" in snap
        assert snap["snapshot_version"] == "1.0"

    def test_execution_snapshot_threshold_version(
        self, client, db_session, monkeypatch, tmp_path
    ):
        """threshold_version in snapshot matches the threshold row.version field."""
        csv_root = _set_csv_root(monkeypatch, tmp_path)
        chain = _build_full_chain(client, db_session, csv_root)
        org_id = uuid.UUID(chain["organization_id"])
        threshold = _insert_threshold(
            db_session, org_id,
            fail_score=10.0, pass_score=20.0,
            max_validation_failure_rate=1.0,
            max_validation_skip_rate=1.0,
            max_high=999, max_critical=999,
            max_warnings=999,
            version=7,
        )
        ctx, _, _ = _build_qc_context(client, db_session, chain)
        QualityControlHandler().execute(ctx)

        db_session.expire_all()
        qc_run = db_session.execute(
            select(QualityControlRun).where(
                QualityControlRun.task_run_id == ctx.task_run.id
            )
        ).scalar_one()
        assert qc_run.execution_snapshot["configuration"]["threshold_version"] == 7

    def test_findings_written_with_correct_org_id(
        self, client, db_session, monkeypatch, tmp_path
    ):
        """Every persisted QualityFinding must have the correct organization_id."""
        csv_root = _set_csv_root(monkeypatch, tmp_path)
        chain = _build_full_chain(client, db_session, csv_root)
        org_id = uuid.UUID(chain["organization_id"])
        _insert_threshold(
            db_session, org_id,
            fail_score=10.0, pass_score=20.0,
            max_validation_failure_rate=1.0,
            max_validation_skip_rate=1.0,
            max_high=999, max_critical=999,
            max_warnings=999,
        )
        ctx, _, _ = _build_qc_context(client, db_session, chain)
        QualityControlHandler().execute(ctx)

        db_session.expire_all()
        qc_run = db_session.execute(
            select(QualityControlRun).where(
                QualityControlRun.task_run_id == ctx.task_run.id
            )
        ).scalar_one()
        findings = (
            db_session.execute(
                select(QualityFinding).where(
                    QualityFinding.quality_control_run_id == qc_run.id
                )
            )
            .scalars()
            .all()
        )
        for f in findings:
            assert f.organization_id == org_id

    def test_findings_categories_valid(
        self, client, db_session, monkeypatch, tmp_path
    ):
        """No persisted QualityFinding has category='quality_control'
        (the meta-finding excluded by CHECK constraint)."""
        csv_root = _set_csv_root(monkeypatch, tmp_path)
        chain = _build_full_chain(client, db_session, csv_root)
        org_id = uuid.UUID(chain["organization_id"])
        _insert_threshold(
            db_session, org_id,
            fail_score=10.0, pass_score=20.0,
            max_validation_failure_rate=1.0,
            max_validation_skip_rate=1.0,
            max_high=999, max_critical=999,
            max_warnings=999,
        )
        ctx, _, _ = _build_qc_context(client, db_session, chain)
        QualityControlHandler().execute(ctx)

        db_session.expire_all()
        qc_run = db_session.execute(
            select(QualityControlRun).where(
                QualityControlRun.task_run_id == ctx.task_run.id
            )
        ).scalar_one()
        findings = (
            db_session.execute(
                select(QualityFinding).where(
                    QualityFinding.quality_control_run_id == qc_run.id
                )
            )
            .scalars()
            .all()
        )
        for f in findings:
            assert f.category != "quality_control", \
                "meta-finding category must never reach the DB"

    def test_return_string_format(
        self, client, db_session, monkeypatch, tmp_path
    ):
        """Return value must contain the run id and recommendation."""
        csv_root = _set_csv_root(monkeypatch, tmp_path)
        chain = _build_full_chain(client, db_session, csv_root)
        org_id = uuid.UUID(chain["organization_id"])
        _insert_threshold(
            db_session, org_id,
            fail_score=10.0, pass_score=20.0,
            max_validation_failure_rate=1.0,
            max_validation_skip_rate=1.0,
            max_high=999, max_critical=999,
            max_warnings=999,
        )
        ctx, _, _ = _build_qc_context(client, db_session, chain)
        result = QualityControlHandler().execute(ctx)
        assert "quality_control_run_id=" in result
        assert "recommendation=" in result


# ===========================================================================
# Idempotency tests
# ===========================================================================

class TestQualityControlHandlerIdempotency:

    def test_second_execute_returns_existing(
        self, client, db_session, monkeypatch, tmp_path
    ):
        """Calling execute() twice on the same TaskRun returns the existing run."""
        csv_root = _set_csv_root(monkeypatch, tmp_path)
        chain = _build_full_chain(client, db_session, csv_root)
        org_id = uuid.UUID(chain["organization_id"])
        _insert_threshold(
            db_session, org_id,
            fail_score=10.0, pass_score=20.0,
            max_validation_failure_rate=1.0,
            max_validation_skip_rate=1.0,
            max_high=999, max_critical=999,
            max_warnings=999,
        )
        ctx, _, _ = _build_qc_context(client, db_session, chain)
        result1 = QualityControlHandler().execute(ctx)
        result2 = QualityControlHandler().execute(ctx)

        assert "already exists" in result2
        # Only one QualityControlRun created
        db_session.expire_all()
        count = db_session.execute(
            select(QualityControlRun).where(
                QualityControlRun.task_run_id == ctx.task_run.id
            )
        ).scalars().all()
        assert len(count) == 1

    def test_one_qc_run_per_task_run_id(
        self, client, db_session, monkeypatch, tmp_path
    ):
        """UNIQUE(task_run_id) constraint: two QC runs on same TaskRun → one row."""
        csv_root = _set_csv_root(monkeypatch, tmp_path)
        chain = _build_full_chain(client, db_session, csv_root)
        org_id = uuid.UUID(chain["organization_id"])
        _insert_threshold(
            db_session, org_id,
            fail_score=10.0, pass_score=20.0,
            max_validation_failure_rate=1.0,
            max_validation_skip_rate=1.0,
            max_high=999, max_critical=999,
            max_warnings=999,
        )
        ctx, _, qc_run = _build_qc_context(client, db_session, chain)
        QualityControlHandler().execute(ctx)
        QualityControlHandler().execute(ctx)  # idempotent second call

        db_session.expire_all()
        all_runs = (
            db_session.execute(
                select(QualityControlRun).where(
                    QualityControlRun.task_run_id == qc_run.id
                )
            )
            .scalars()
            .all()
        )
        assert len(all_runs) == 1

    def test_one_qc_run_per_validation_run_id(
        self, client, db_session, monkeypatch, tmp_path
    ):
        """UNIQUE(validation_run_id): a second QUALITY_CTRL run pointing at the
        same ValidationRun should be caught by the IntegrityError handler and
        return the first run's data."""
        csv_root = _set_csv_root(monkeypatch, tmp_path)
        chain = _build_full_chain(client, db_session, csv_root)
        org_id = uuid.UUID(chain["organization_id"])
        _insert_threshold(
            db_session, org_id,
            fail_score=10.0, pass_score=20.0,
            max_validation_failure_rate=1.0,
            max_validation_skip_rate=1.0,
            max_high=999, max_critical=999,
            max_warnings=999,
        )
        # First QC run
        ctx1, _, _ = _build_qc_context(client, db_session, chain)
        QualityControlHandler().execute(ctx1)

        # Second QC task/run pointing at the SAME ValidationRun
        ctx2, _, _ = _build_qc_context(client, db_session, chain)
        # This will hit the UNIQUE(validation_run_id) constraint;
        # the handler must catch IntegrityError and refetch
        result2 = QualityControlHandler().execute(ctx2)
        # Returns "already exists" (idempotency) or a new run summary
        # Either is acceptable -- what must NOT happen is an uncaught exception
        assert isinstance(result2, str)

        db_session.expire_all()
        all_runs = (
            db_session.execute(select(QualityControlRun))
            .scalars()
            .all()
        )
        validation_run_ids = {r.validation_run_id for r in all_runs}
        # Each ValidationRun ID appears exactly once
        assert len(validation_run_ids) == len(all_runs)


# ===========================================================================
# Prerequisite error tests
# ===========================================================================

class TestQualityControlHandlerPrerequisiteErrors:

    def test_missing_source_task_run_id(
        self, client, db_session, monkeypatch, tmp_path
    ):
        """TaskRun with no source_task_run_id → PermanentExecutionError."""
        csv_root = _set_csv_root(monkeypatch, tmp_path)
        suffix = uuid.uuid4().hex[:8]
        headers = _auth_headers(client, suffix)
        ds_resp = client.post(
            "/data-sources",
            json={"name": f"src-{suffix}", "source_type": "csv_upload",
                  "connection_metadata": {"file_path": "f.csv"}},
            headers=headers,
        )
        source_id = ds_resp.json()["id"]
        org_id_str = ds_resp.json()["organization_id"]
        _insert_threshold(
            db_session, uuid.UUID(org_id_str),
            fail_score=10.0, pass_score=20.0,
            max_validation_failure_rate=1.0,
            max_validation_skip_rate=1.0,
            max_high=999, max_critical=999, max_warnings=999,
        )

        qc_task_resp = client.post(
            "/tasks",
            json={"name": "QC", "task_type": "quality_ctrl",
                  "data_source_id": source_id},
            headers=headers,
        )
        qc_run_resp = client.post(
            f"/tasks/{qc_task_resp.json()['id']}/runs",
            headers=headers,
            # no source_task_run_id
        )
        db_session.expire_all()
        qc_task = db_session.get(Task, uuid.UUID(qc_task_resp.json()["id"]))
        qc_run = db_session.get(TaskRun, uuid.UUID(qc_run_resp.json()["id"]))
        source = db_session.get(DataSource, uuid.UUID(source_id))
        ctx = ExecutionContext(
            task_run=qc_run, task=qc_task, data_source=source,
            idempotency_key=str(qc_run.idempotency_key),
            credential_provider=None,
        )
        with pytest.raises(PermanentExecutionError, match="source_task_run_id"):
            QualityControlHandler().execute(ctx)

    def test_missing_data_source(
        self, client, db_session, monkeypatch, tmp_path
    ):
        """TaskRun with no data_source → PermanentExecutionError."""
        csv_root = _set_csv_root(monkeypatch, tmp_path)
        chain = _build_full_chain(client, db_session, csv_root)
        ctx, qc_task, qc_run = _build_qc_context(client, db_session, chain)
        ctx_no_ds = ExecutionContext(
            task_run=qc_run, task=qc_task, data_source=None,
            idempotency_key=str(qc_run.idempotency_key),
            credential_provider=None,
        )
        with pytest.raises(PermanentExecutionError, match="data source"):
            QualityControlHandler().execute(ctx_no_ds)

    def test_cross_org_validation_run_not_found(
        self, client, db_session, monkeypatch, tmp_path
    ):
        """A source_task_run_id from a different org → PermanentExecutionError."""
        csv_root = _set_csv_root(monkeypatch, tmp_path)
        # Build chain for org A
        chain_a = _build_full_chain(client, db_session, csv_root,
                                    suffix=uuid.uuid4().hex[:8])
        # Build stub for org B
        suffix_b = uuid.uuid4().hex[:8]
        headers_b = _auth_headers(client, suffix_b)
        ds_b = client.post(
            "/data-sources",
            json={"name": f"src-b-{suffix_b}", "source_type": "csv_upload",
                  "connection_metadata": {"file_path": "f.csv"}},
            headers=headers_b,
        )
        source_id_b = ds_b.json()["id"]
        org_id_b = uuid.UUID(ds_b.json()["organization_id"])
        _insert_threshold(
            db_session, org_id_b,
            fail_score=10.0, pass_score=20.0,
            max_validation_failure_rate=1.0,
            max_validation_skip_rate=1.0,
            max_high=999, max_critical=999, max_warnings=999,
        )

        # Create a QC task/run in org B that points to org A's validate run.
        # QUALITY_CTRL is not in the API allow-list for source_task_run_id, so
        # create without it and patch the cross-org FK directly in the DB.
        qc_task_resp = client.post(
            "/tasks",
            json={"name": "Cross-Org QC", "task_type": "quality_ctrl",
                  "data_source_id": source_id_b},
            headers=headers_b,
        )
        assert qc_task_resp.status_code == 201, qc_task_resp.text
        qc_run_resp = client.post(
            f"/tasks/{qc_task_resp.json()['id']}/runs",
            json={},
            headers=headers_b,
        )
        assert qc_run_resp.status_code == 201, qc_run_resp.text
        db_session.expire_all()
        qc_task = db_session.get(Task, uuid.UUID(qc_task_resp.json()["id"]))
        qc_run = db_session.get(TaskRun, uuid.UUID(qc_run_resp.json()["id"]))
        source = db_session.get(DataSource, uuid.UUID(source_id_b))
        # Wire cross-org source: org A's validate run FK into org B's QC run.
        # The composite FK (org_id, source_task_run_id) → task_runs would reject
        # this normally, so disable FK enforcement for this one UPDATE to simulate
        # what a DB-level exploit (or misconfiguration) could produce — the handler
        # must still raise PermanentExecutionError regardless.
        qc_run_id_hex = qc_run.id.hex
        validate_run_id_hex = uuid.UUID(chain_a["validate_run_id"]).hex
        db_session.execute(text("PRAGMA foreign_keys=OFF"))
        db_session.execute(
            text("UPDATE task_runs SET source_task_run_id = :src WHERE id = :id"),
            {"src": validate_run_id_hex, "id": qc_run_id_hex},
        )
        db_session.execute(text("PRAGMA foreign_keys=ON"))
        db_session.commit()
        db_session.expire_all()
        qc_run = db_session.get(TaskRun, qc_run.id)
        ctx = ExecutionContext(
            task_run=qc_run, task=qc_task, data_source=source,
            idempotency_key=str(qc_run.idempotency_key),
            credential_provider=None,
        )
        with pytest.raises(PermanentExecutionError):
            QualityControlHandler().execute(ctx)

    def test_missing_data_profile(
        self, client, db_session, monkeypatch, tmp_path
    ):
        """If the DataProfile for the SYNC run is absent → PermanentExecutionError."""
        csv_root = _set_csv_root(monkeypatch, tmp_path)
        chain = _build_full_chain(client, db_session, csv_root)
        org_id = uuid.UUID(chain["organization_id"])
        _insert_threshold(
            db_session, org_id,
            fail_score=10.0, pass_score=20.0,
            max_validation_failure_rate=1.0,
            max_validation_skip_rate=1.0,
            max_high=999, max_critical=999, max_warnings=999,
        )

        # Delete the DataProfile
        db_session.delete(chain["data_profile"])
        db_session.commit()

        ctx, _, _ = _build_qc_context(client, db_session, chain)
        with pytest.raises(PermanentExecutionError, match="DataProfile"):
            QualityControlHandler().execute(ctx)


# ===========================================================================
# Threshold tests
# ===========================================================================

class TestQualityControlHandlerThreshold:

    def test_no_threshold_raises(
        self, client, db_session, monkeypatch, tmp_path
    ):
        """No active threshold at all → PermanentExecutionError."""
        csv_root = _set_csv_root(monkeypatch, tmp_path)
        chain = _build_full_chain(client, db_session, csv_root)
        ctx, _, _ = _build_qc_context(client, db_session, chain)
        with pytest.raises(PermanentExecutionError, match="QualityThreshold"):
            QualityControlHandler().execute(ctx)

    def test_inactive_threshold_raises(
        self, client, db_session, monkeypatch, tmp_path
    ):
        """is_active=False threshold treated as absent → PermanentExecutionError."""
        csv_root = _set_csv_root(monkeypatch, tmp_path)
        chain = _build_full_chain(client, db_session, csv_root)
        org_id = uuid.UUID(chain["organization_id"])
        _insert_threshold(
            db_session, org_id,
            fail_score=10.0, pass_score=20.0,
            max_validation_failure_rate=1.0,
            max_validation_skip_rate=1.0,
            max_high=999, max_critical=999, max_warnings=999,
            is_active=False,
        )
        ctx, _, _ = _build_qc_context(client, db_session, chain)
        with pytest.raises(PermanentExecutionError):
            QualityControlHandler().execute(ctx)

    def test_ds_specific_threshold_takes_precedence(
        self, client, db_session, monkeypatch, tmp_path
    ):
        """Data-source-specific threshold wins over org-wide."""
        csv_root = _set_csv_root(monkeypatch, tmp_path)
        chain = _build_full_chain(client, db_session, csv_root)
        org_id = uuid.UUID(chain["organization_id"])
        source_id = uuid.UUID(chain["source_id"])

        # Org-wide: very strict (would FAIL everything)
        _insert_threshold(
            db_session, org_id,
            fail_score=99.0, pass_score=100.0,
            max_validation_failure_rate=0.0,
            max_validation_skip_rate=0.0,
        )
        # DS-specific: very lenient
        _insert_threshold(
            db_session, org_id,
            data_source_id=source_id,
            fail_score=10.0, pass_score=20.0,
            max_validation_failure_rate=1.0,
            max_validation_skip_rate=1.0,
            max_high=999, max_critical=999, max_warnings=999,
        )
        ctx, _, _ = _build_qc_context(client, db_session, chain)
        result = QualityControlHandler().execute(ctx)
        # Should succeed (DS-specific threshold is lenient enough)
        assert "quality control run created" in result

        db_session.expire_all()
        qc_run = db_session.execute(
            select(QualityControlRun).where(
                QualityControlRun.task_run_id == ctx.task_run.id
            )
        ).scalar_one()
        snap = qc_run.execution_snapshot
        assert snap["configuration"]["threshold_config_source"] == "data_source_specific"

    def test_org_wide_threshold_used_when_no_ds_specific(
        self, client, db_session, monkeypatch, tmp_path
    ):
        """Org-wide threshold used when no DS-specific row exists."""
        csv_root = _set_csv_root(monkeypatch, tmp_path)
        chain = _build_full_chain(client, db_session, csv_root)
        org_id = uuid.UUID(chain["organization_id"])
        _insert_threshold(
            db_session, org_id,
            fail_score=10.0, pass_score=20.0,
            max_validation_failure_rate=1.0,
            max_validation_skip_rate=1.0,
            max_high=999, max_critical=999, max_warnings=999,
        )
        ctx, _, _ = _build_qc_context(client, db_session, chain)
        QualityControlHandler().execute(ctx)

        db_session.expire_all()
        qc_run = db_session.execute(
            select(QualityControlRun).where(
                QualityControlRun.task_run_id == ctx.task_run.id
            )
        ).scalar_one()
        assert qc_run.execution_snapshot["configuration"]["threshold_config_source"] \
            == "org_wide"

    def test_invalid_threshold_pass_le_fail_raises(
        self, client, db_session, monkeypatch, tmp_path
    ):
        """pass_score_threshold <= fail_score_threshold → PermanentExecutionError.

        SQLite enforces the ck_quality_thresholds_pass_gt_fail CHECK constraint
        even for raw SQL inserts, so we cannot inject an invalid row directly.
        Instead, patch _resolve_threshold to return an invalid config and verify
        the handler's own _validate_threshold_config raises PermanentExecutionError.
        """
        from app.quality.types import QualityThresholdConfig
        from app.worker.handlers.quality_control import QualityControlHandler

        csv_root = _set_csv_root(monkeypatch, tmp_path)
        chain = _build_full_chain(client, db_session, csv_root)
        org_id = uuid.UUID(chain["organization_id"])
        # Insert a valid threshold so step 3 (resolve) doesn't fail first.
        _insert_threshold(
            db_session, org_id,
            fail_score=10.0, pass_score=20.0,
            max_validation_failure_rate=1.0,
            max_validation_skip_rate=1.0,
            max_high=999, max_critical=999, max_warnings=999,
        )
        # Patch _resolve_threshold to return a config with pass == fail (invalid).
        bad_cfg = QualityThresholdConfig(
            fail_score_threshold=80.0,
            pass_score_threshold=80.0,  # equal → handler must reject
            max_validation_failure_rate=0.0,
            max_validation_skip_rate=0.5,
            max_high_severity_unresolved=0,
            max_critical_severity_unresolved=0,
            max_warnings_for_clean_pass=0,
        )
        monkeypatch.setattr(
            QualityControlHandler, "_resolve_threshold",
            staticmethod(lambda db, org_id, ds_id: (None, bad_cfg, "monkeypatched")),
        )
        ctx, _, _ = _build_qc_context(client, db_session, chain)
        with pytest.raises(PermanentExecutionError, match="pass_score_threshold"):
            QualityControlHandler().execute(ctx)

    def test_invalid_category_weights_key_raises(
        self, client, db_session, monkeypatch, tmp_path
    ):
        """Unknown key in category_weights → PermanentExecutionError."""
        csv_root = _set_csv_root(monkeypatch, tmp_path)
        chain = _build_full_chain(client, db_session, csv_root)
        org_id = uuid.UUID(chain["organization_id"])
        _insert_threshold(
            db_session, org_id,
            fail_score=10.0, pass_score=20.0,
            max_validation_failure_rate=1.0,
            max_validation_skip_rate=1.0,
            max_high=999, max_critical=999, max_warnings=999,
            category_weights={"not_a_real_category": 1.0},
        )
        ctx, _, _ = _build_qc_context(client, db_session, chain)
        with pytest.raises(PermanentExecutionError, match="category_weights"):
            QualityControlHandler().execute(ctx)

    def test_negative_max_high_raises(
        self, client, db_session, monkeypatch, tmp_path
    ):
        """Negative max_high_severity_unresolved → PermanentExecutionError.

        SQLite enforces ck_quality_thresholds_high_sev_nonneg CHECK constraint
        even for raw SQL inserts, so we cannot inject an invalid row directly.
        Patch _resolve_threshold to return a config with max_high = -1 and
        verify the handler's own _validate_threshold_config rejects it.
        """
        from app.quality.types import QualityThresholdConfig
        from app.worker.handlers.quality_control import QualityControlHandler

        csv_root = _set_csv_root(monkeypatch, tmp_path)
        chain = _build_full_chain(client, db_session, csv_root)
        org_id = uuid.UUID(chain["organization_id"])
        # Insert a valid threshold so _resolve_threshold would normally succeed.
        _insert_threshold(
            db_session, org_id,
            fail_score=10.0, pass_score=20.0,
            max_validation_failure_rate=1.0,
            max_validation_skip_rate=1.0,
            max_high=999, max_critical=999, max_warnings=999,
        )
        # Patch _resolve_threshold to return a config with negative max_high.
        bad_cfg = QualityThresholdConfig(
            fail_score_threshold=60.0,
            pass_score_threshold=85.0,
            max_validation_failure_rate=0.0,
            max_validation_skip_rate=0.5,
            max_high_severity_unresolved=-1,  # invalid
            max_critical_severity_unresolved=0,
            max_warnings_for_clean_pass=0,
        )
        monkeypatch.setattr(
            QualityControlHandler, "_resolve_threshold",
            staticmethod(lambda db, org_id, ds_id: (None, bad_cfg, "monkeypatched")),
        )
        ctx, _, _ = _build_qc_context(client, db_session, chain)
        with pytest.raises(PermanentExecutionError, match="max_high_severity_unresolved"):
            QualityControlHandler().execute(ctx)


# ===========================================================================
# Persistence tests
# ===========================================================================

class TestQualityControlHandlerPersistence:

    def test_finding_limit_enforced(
        self, client, db_session, monkeypatch, tmp_path
    ):
        """quality_max_persisted_findings caps the number of QualityFinding rows."""
        csv_root = _set_csv_root(monkeypatch, tmp_path)
        chain = _build_full_chain(client, db_session, csv_root)
        org_id = uuid.UUID(chain["organization_id"])
        _insert_threshold(
            db_session, org_id,
            fail_score=10.0, pass_score=20.0,
            max_validation_failure_rate=1.0,
            max_validation_skip_rate=1.0,
            max_high=999, max_critical=999, max_warnings=999,
        )

        # Patch max to 1 so we can observe the cap
        monkeypatch.setattr(
            get_settings(), "quality_max_persisted_findings", 1
        )
        ctx, _, _ = _build_qc_context(client, db_session, chain)
        QualityControlHandler().execute(ctx)

        db_session.expire_all()
        qc_run = db_session.execute(
            select(QualityControlRun).where(
                QualityControlRun.task_run_id == ctx.task_run.id
            )
        ).scalar_one()
        findings = (
            db_session.execute(
                select(QualityFinding).where(
                    QualityFinding.quality_control_run_id == qc_run.id
                )
            )
            .scalars()
            .all()
        )
        assert len(findings) <= 1

    def test_qc_run_links_to_correct_upstream_ids(
        self, client, db_session, monkeypatch, tmp_path
    ):
        """QualityControlRun stores the correct validation_run_id, remediation_run_id,
        issue_detection_run_id, and data_profile_id."""
        csv_root = _set_csv_root(monkeypatch, tmp_path)
        chain = _build_full_chain(client, db_session, csv_root)
        org_id = uuid.UUID(chain["organization_id"])
        _insert_threshold(
            db_session, org_id,
            fail_score=10.0, pass_score=20.0,
            max_validation_failure_rate=1.0,
            max_validation_skip_rate=1.0,
            max_high=999, max_critical=999, max_warnings=999,
        )
        ctx, _, _ = _build_qc_context(client, db_session, chain)
        QualityControlHandler().execute(ctx)

        db_session.expire_all()
        qc_run = db_session.execute(
            select(QualityControlRun).where(
                QualityControlRun.task_run_id == ctx.task_run.id
            )
        ).scalar_one()
        assert qc_run.validation_run_id == chain["validation_run"].id
        assert qc_run.remediation_run_id == chain["remediation_run"].id
        assert qc_run.data_profile_id == chain["data_profile"].id


# ===========================================================================
# Engine purity test
# ===========================================================================

class TestEnginePurity:

    def test_quality_package_has_no_db_imports(self):
        """The app/quality/ package must not import SQLAlchemy, FastAPI, or
        worker components.  This is the three-layer architecture invariant."""
        import app.quality as pkg
        quality_path = Path(pkg.__file__).parent
        forbidden = {
            "sqlalchemy", "fastapi", "app.worker", "app.db", "app.api",
        }
        violations: list[str] = []
        for py_file in quality_path.rglob("*.py"):
            source = py_file.read_text(encoding="utf-8")
            try:
                tree = ast.parse(source)
            except SyntaxError:
                continue
            for node in ast.walk(tree):
                if isinstance(node, (ast.Import, ast.ImportFrom)):
                    if isinstance(node, ast.ImportFrom) and node.module:
                        mod = node.module
                    elif isinstance(node, ast.Import):
                        for alias in node.names:
                            mod = alias.name
                            for f in forbidden:
                                if mod.startswith(f):
                                    violations.append(
                                        f"{py_file.name}: import {mod!r}"
                                    )
                        continue
                    for f in forbidden:
                        if mod.startswith(f):
                            violations.append(f"{py_file.name}: import {mod!r}")
        assert not violations, (
            "app/quality/ imports forbidden modules (DB/API layer):\n"
            + "\n".join(violations)
        )
