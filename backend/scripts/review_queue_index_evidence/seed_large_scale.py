"""Module 11 index-evidence seed script (deliberate large-tenant pass).

Standalone, manual-only -- never imported by the application or the test
suite. See README.md in this directory for full usage, expected
PostgreSQL version, and cleanup instructions.

Must be run AFTER seed_representative.py, against the same database.
Looks up "Evidence Org 0" (created by that script, slug 'evidence-org-0')
and adds ~20,000 additional cleaning_runs rows (and their backing
task_runs) to it -- the large-tenant scenario that showed a measurable
Seq Scan -> Bitmap Heap Scan improvement from the
ix_cleaning_runs_org_status composite index (see the migration's own
docstring for the reported numbers).

Requires:
- DATABASE_URL set in the environment, pointing at the same disposable
  database seed_representative.py was run against.
- seed_representative.py already run against that database.

Never run against a production or shared database.
"""
import os
import pathlib
import random
import sys
import uuid
from datetime import datetime, timedelta, timezone

_BACKEND_ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_BACKEND_ROOT))

if "DATABASE_URL" not in os.environ:
    raise SystemExit(
        "DATABASE_URL is not set. Point it at the same disposable "
        "PostgreSQL database seed_representative.py was run against -- "
        "see README.md."
    )

from sqlalchemy import create_engine, insert, select  # noqa: E402

from app.models.cleaning_run import CleaningRun  # noqa: E402
from app.models.data_source import DataSource  # noqa: E402
from app.models.organization import Organization  # noqa: E402
from app.models.task import Task  # noqa: E402
from app.models.task_run import TaskRun  # noqa: E402

random.seed(7)
engine = create_engine(os.environ["DATABASE_URL"])
now = datetime.now(timezone.utc)

RUN_STATUS_DIST = [("pending_review", 0.15), ("approved", 0.70), ("rejected", 0.10), ("rolled_back", 0.05)]


def pick(dist):
    r = random.random()
    acc = 0.0
    for val, p in dist:
        acc += p
        if r <= acc:
            return val
    return dist[-1][0]


N = 20000

with engine.begin() as conn:
    org_id = conn.execute(
        select(Organization.id).where(Organization.slug == "evidence-org-0")
    ).scalar_one_or_none()
    if org_id is None:
        raise SystemExit(
            "'Evidence Org 0' (slug='evidence-org-0') not found. Run "
            "seed_representative.py against this database first."
        )
    task_id = conn.execute(select(Task.id).where(Task.organization_id == org_id).limit(1)).scalar_one()
    data_source_id = conn.execute(
        select(DataSource.id).where(DataSource.organization_id == org_id).limit(1)
    ).scalar_one()

    task_run_ids = [uuid.uuid4() for _ in range(N)]
    batch = []
    for tr_id in task_run_ids:
        batch.append(dict(
            id=tr_id, organization_id=org_id, task_id=task_id, status="success",
            started_at=now, finished_at=now, idempotency_key=uuid.uuid4(), attempt_count=1,
            created_at=now,
        ))
        if len(batch) >= 2000:
            conn.execute(insert(TaskRun), batch)
            batch = []
    if batch:
        conn.execute(insert(TaskRun), batch)
print(f"Inserted {N} backing task_runs for org {org_id}")

batch = []
with engine.begin() as conn:
    for i in range(N):
        batch.append(dict(
            id=uuid.uuid4(), organization_id=org_id, task_run_id=task_run_ids[i],
            task_id=task_id, data_source_id=data_source_id,
            source_task_run_id=task_run_ids[i],
            output_file_path=f"/tenant/{org_id}/out{i}.csv", output_sha256="b" * 64,
            row_count=1000, total_changes_count=50, changes_by_rule={},
            duplicate_row_count=5, confidence_score=round(random.uniform(0.5, 1.0), 4),
            post_clean_row_count=995, post_clean_missing_value_total=0,
            post_clean_duplicate_row_count=0, cleaning_engine_version="1.0",
            status=pick(RUN_STATUS_DIST),
            created_at=now - timedelta(seconds=random.randint(0, 60 * 86400)),
        ))
        if len(batch) >= 2000:
            conn.execute(insert(CleaningRun), batch)
            batch = []
    if batch:
        conn.execute(insert(CleaningRun), batch)
print(f"Inserted {N} large-scale cleaning_runs rows for org {org_id}")
print("Org id for EXPLAIN queries:", str(org_id))
