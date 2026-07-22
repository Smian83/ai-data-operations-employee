"""Module 11 index-evidence seed script (near-term-scale pass).

Standalone, manual-only -- never imported by the application or the test
suite. See README.md in this directory for full usage, expected
PostgreSQL version, and cleanup instructions.

Seeds 3 organizations ("Evidence Org 0/1/2") with several hundred rows
per source table per organization, at a realistic (non-uniform)
status/decision/outcome distribution -- this is the near-term-scale pass
that showed the existing organization_id-only index was already
sufficient (see the migration's own docstring). Run
scripts/review_queue_index_evidence/seed_large_scale.py afterward, against
the same database, to reproduce the deliberate large-tenant pass that did
show a measurable benefit from the composite indexes.

Requires:
- DATABASE_URL set in the environment, pointing at a disposable database.
- `alembic upgrade head` already run against that database.

Never run against a production or shared database.
"""
import os
import pathlib
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone

# Locate the backend/ package relative to this file's own location
# (scripts/review_queue_index_evidence/seed_representative.py -> backend/)
# rather than any machine- or sandbox-specific absolute path.
_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_BACKEND_ROOT))

if "DATABASE_URL" not in os.environ:
    raise SystemExit(
        "DATABASE_URL is not set. Point it at a disposable PostgreSQL "
        "database before running this script -- see README.md."
    )

from sqlalchemy import create_engine, insert  # noqa: E402

from app.models.artifact_download_event import ArtifactDownloadEvent  # noqa: E402
from app.models.cleaning_run import CleaningRun  # noqa: E402
from app.models.data_source import DataSource  # noqa: E402
from app.models.export_run import ExportRun  # noqa: E402
from app.models.match_decision import MatchDecision  # noqa: E402
from app.models.match_run import MatchRun  # noqa: E402
from app.models.organization import Organization  # noqa: E402
from app.models.standardization_run import StandardizationRun  # noqa: E402
from app.models.task import Task  # noqa: E402
from app.models.task_run import TaskRun  # noqa: E402
from app.models.user import User  # noqa: E402

random.seed(42)
engine = create_engine(os.environ["DATABASE_URL"])

now = datetime.now(timezone.utc)


def rand_time(days_back=60):
    return now - timedelta(seconds=random.randint(0, days_back * 86400))


N_ORGS = 3
RUNS_PER_ORG = {
    "task_run": 600,
    "cleaning_run": 250,
    "standardization_run": 250,
    "match_run": 250,
    "export_run": 250,
    "match_decision": 400,
    "artifact_download_event": 200,
}

RUN_STATUS_DIST = [("pending_review", 0.15), ("approved", 0.70), ("rejected", 0.10), ("rolled_back", 0.05)]
TASK_RUN_STATUS_DIST = [("pending", 0.03), ("running", 0.02), ("success", 0.85), ("failed", 0.10)]
DECISION_DIST = [("duplicate", 0.80), ("ambiguous", 0.20)]
OUTCOME_DIST = [
    ("completed", 0.70), ("integrity_failed", 0.10), ("file_missing", 0.08),
    ("stream_failed", 0.07), ("started", 0.05),
]


def pick(dist):
    r = random.random()
    acc = 0.0
    for val, p in dist:
        acc += p
        if r <= acc:
            return val
    return dist[-1][0]


with engine.begin() as conn:
    org_ids = []
    for i in range(N_ORGS):
        org_id = uuid.uuid4()
        org_ids.append(org_id)
        conn.execute(insert(Organization).values(
            id=org_id, name=f"Evidence Org {i}", slug=f"evidence-org-{i}",
        ))
        user_id = uuid.uuid4()
        conn.execute(insert(User).values(
            id=user_id, organization_id=org_id, email=f"user{i}@evidence.test",
            hashed_password="x", is_active=True, is_superuser=False,
        ))

    for org_id in org_ids:
        ds_id = uuid.uuid4()
        conn.execute(insert(DataSource).values(
            id=ds_id, organization_id=org_id, name="Evidence Dataset",
            source_type="csv_upload", connection_metadata={}, is_active=True,
        ))
        task_ids = []
        for tt in ["sync", "transform", "standardize", "match", "export"]:
            task_id = uuid.uuid4()
            task_ids.append(task_id)
            conn.execute(insert(Task).values(
                id=task_id, organization_id=org_id, data_source_id=ds_id,
                name=f"Evidence Task {tt}", task_type=tt, is_active=True,
            ))

        task_run_ids = []
        for _ in range(RUNS_PER_ORG["task_run"]):
            tr_id = uuid.uuid4()
            task_run_ids.append(tr_id)
            status = pick(TASK_RUN_STATUS_DIST)
            started = rand_time() if status != "pending" else None
            finished = (
                started + timedelta(seconds=random.randint(1, 600))
                if started and status in ("success", "failed") else None
            )
            conn.execute(insert(TaskRun).values(
                id=tr_id, organization_id=org_id, task_id=random.choice(task_ids),
                status=status, started_at=started, finished_at=finished,
                error_message="Simulated processing failure" if status == "failed" else None,
                idempotency_key=uuid.uuid4(), attempt_count=1,
                lease_token=uuid.uuid4() if status == "running" else None,
                lease_expires_at=(now + timedelta(minutes=5)) if status == "running" else None,
                created_at=rand_time(),
            ))

        def base_run_kwargs(tr_id):
            return dict(
                id=uuid.uuid4(), organization_id=org_id, task_run_id=tr_id,
                task_id=random.choice(task_ids), data_source_id=ds_id,
                source_task_run_id=random.choice(task_run_ids),
                output_file_path=f"/tenant/{org_id}/out.csv", output_sha256="a" * 64,
                confidence_score=round(random.uniform(0.5, 1.0), 4),
                status=pick(RUN_STATUS_DIST), created_at=rand_time(),
            )

        cleaning_tr_ids = random.sample(task_run_ids, RUNS_PER_ORG["cleaning_run"])
        for tr_id in cleaning_tr_ids:
            conn.execute(insert(CleaningRun).values(
                **base_run_kwargs(tr_id),
                row_count=1000, total_changes_count=50, changes_by_rule={},
                duplicate_row_count=5, post_clean_row_count=995,
                post_clean_missing_value_total=0, post_clean_duplicate_row_count=0,
                cleaning_engine_version="1.0",
            ))

        standardization_tr_ids = random.sample(task_run_ids, RUNS_PER_ORG["standardization_run"])
        for tr_id in standardization_tr_ids:
            conn.execute(insert(StandardizationRun).values(
                **base_run_kwargs(tr_id),
                row_count=1000, total_changes_count=50, changes_by_rule={},
                standardization_engine_version="1.0",
            ))

        match_run_ids = []
        match_tr_ids = random.sample(task_run_ids, RUNS_PER_ORG["match_run"])
        for tr_id in match_tr_ids:
            mr_id = uuid.uuid4()
            match_run_ids.append(mr_id)
            kw = base_run_kwargs(tr_id)
            kw["id"] = mr_id
            kw.pop("output_file_path")
            kw.pop("output_sha256")
            conn.execute(insert(MatchRun).values(
                **kw,
                row_count=1000, total_comparisons_count=2000,
                duplicate_group_count=20, duplicate_pairs_count=30,
                ambiguous_pairs_count=10, skipped_block_count=0,
                decisions_by_rule={}, match_engine_version="1.0",
            ))

        export_tr_ids = random.sample(task_run_ids, RUNS_PER_ORG["export_run"])
        for tr_id in export_tr_ids:
            kw = base_run_kwargs(tr_id)
            kw["match_run_id"] = random.choice(match_run_ids)
            kw.pop("confidence_score")
            conn.execute(insert(ExportRun).values(
                **kw,
                source_row_count=1000, row_count=980, excluded_row_count=20,
                duplicate_groups_materialized_count=20,
                output_file_size_bytes=50000, output_column_count=10,
                export_timestamp=rand_time(), csv_format_version=1,
                export_engine_version="1.0",
            ))

        for _ in range(RUNS_PER_ORG["match_decision"]):
            decision = pick(DECISION_DIST)
            conn.execute(insert(MatchDecision).values(
                id=uuid.uuid4(), organization_id=org_id,
                match_run_id=random.choice(match_run_ids),
                match_group_id=None,
                record_a_row_index=random.randint(0, 900),
                record_b_row_index=random.randint(901, 999),
                rule_name="exact_row_match", field_comparisons={},
                total_score=round(random.uniform(0.5, 1.0), 4),
                threshold_used=0.8, decision=decision,
                confidence_score=round(random.uniform(0.5, 1.0), 4),
                reason="Simulated match reason", rule_version="1.0",
                created_at=rand_time(),
            ))

        cleaning_run_pks = conn.execute(
            CleaningRun.__table__.select().where(CleaningRun.organization_id == org_id)
        ).fetchall()
        standardization_run_pks = conn.execute(
            StandardizationRun.__table__.select().where(StandardizationRun.organization_id == org_id)
        ).fetchall()
        export_run_pks = conn.execute(
            ExportRun.__table__.select().where(ExportRun.organization_id == org_id)
        ).fetchall()

        for _ in range(RUNS_PER_ORG["artifact_download_event"]):
            branch = random.choice(["cleaning", "standardization", "export"])
            if branch == "cleaning" and cleaning_run_pks:
                row = random.choice(cleaning_run_pks)
                artifact_type, run_col, run_id = "cleaning", "cleaning_run_id", row.id
            elif branch == "standardization" and standardization_run_pks:
                row = random.choice(standardization_run_pks)
                artifact_type, run_col, run_id = "standardization", "standardization_run_id", row.id
            elif export_run_pks:
                row = random.choice(export_run_pks)
                artifact_type, run_col, run_id = "export", "export_run_id", row.id
            else:
                continue
            outcome = pick(OUTCOME_DIST)
            values = dict(
                id=uuid.uuid4(), organization_id=org_id, artifact_type=artifact_type,
                run_status_at_request="approved", outcome=outcome,
                failure_reason_code={
                    "integrity_failed": "hash_mismatch",
                    "file_missing": "file_not_found",
                    "stream_failed": "io_error",
                }.get(outcome),
                verified_sha256="a" * 64 if outcome not in ("started", "file_missing") else None,
                bytes_served=50000 if outcome == "completed" else 0,
                created_at=rand_time(),
                completed_at=rand_time() if outcome != "started" else None,
            )
            values[run_col] = run_id
            conn.execute(insert(ArtifactDownloadEvent).values(**values))

print("Seed complete.")
print("org_ids:", [str(o) for o in org_ids])
print("'Evidence Org 0' id (used by seed_large_scale.py):", str(org_ids[0]))
