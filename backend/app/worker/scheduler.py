"""
Scheduled task execution (Module 12).

Gives Task.schedule_interval_seconds real, executable meaning. This module
does exactly one thing, in a bounded loop, once per poll:

    Task becomes due
    -> atomically create exactly one pending TaskRun
    -> advance next_run_at
    -> stop

It never executes anything. It never calls a handler. It never touches
approval/rejection/rollback state. The existing claim/lease/execute engine
(app/worker/engine.py) remains the one and only execution path -- a
scheduler-created TaskRun is claimed and run by claim_batch() exactly like
a manually-created one, indistinguishable except for triggered_by IS NULL.
V1 is SYNC-only, enforced upstream at the API layer (app/api/tasks.py) --
this module's own WHERE clause additionally never selects a non-SYNC task,
since only SYNC tasks can ever have schedule_interval_seconds set at all.

Transaction design: each due task is claimed, advanced, and given its
TaskRun inside its OWN short-lived transaction -- not one transaction for
the whole batch. A crash or error on one task only ever rolls back that
one task; every other task already committed earlier in the same pass
keeps its progress (see the module's design doc, Section 17-19, for the
full comparison against a whole-batch-transaction alternative and why it
was rejected).

Concurrency safety uses the identical two-layer pattern already proven in
engine.py::claim_batch and reaper.py::reap_expired_runs: a `SELECT ... FOR
UPDATE SKIP LOCKED` claim (degrading to a plain SELECT on SQLite, see
_supports_skip_locked), plus a guarded UPDATE whose WHERE clause re-checks
the row's prior state. Two concurrent scheduler passes can never create two
TaskRuns for the same due occurrence.

Fault isolation / starvation avoidance: if a due task's per-task
transaction fails (an unexpected exception, or losing the guarded-update
race), it is added to a pass-local, in-memory `excluded_ids` set and never
re-selected for the remainder of THIS pass -- so one permanently malformed
task costs at most one wasted slot in the batch, not the whole batch, and
never blocks any other due task from being processed in the same pass. No
persistent state or new table is used for this; the set is discarded when
the pass returns.

Missed-schedule policy: next_run_at is always recomputed as
`claim_time + interval`, anchored to the moment of claiming, never to the
old (possibly long-stale) next_run_at. Any number of missed periods
collapses into exactly one catch-up TaskRun per task per pass -- never a
catch-up storm.
"""
import logging
import time
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.task import Task
from app.services.task_run_factory import create_task_run_record
from app.worker import metrics
from app.worker.engine import _supports_skip_locked

logger = logging.getLogger(__name__)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def run_due_schedules(
    db: Session, worker_id: str = "scheduler", batch_size: int | None = None
) -> int:
    """Process up to `batch_size` due, active, scheduled Tasks: create one
    pending TaskRun per task and advance its next_run_at, each inside its
    own committed transaction. Returns the number of TaskRuns created.

    Increments scheduler_passes_total once, unconditionally, at the start
    (this counts "how many times the scheduler ran," not "how many
    succeeded with zero row-level errors" -- a pass can legitimately
    contain isolated per-task failures while still completing overall).
    scheduler_last_success_timestamp_seconds is only updated if this
    function returns normally (including on an empty pass with zero due
    tasks) -- not on an unhandled, pass-level exception.
    """
    settings = get_settings()
    if batch_size is None:
        batch_size = settings.scheduler_claim_batch_size
    metrics.scheduler_passes_total.inc()

    now = _now()
    created = 0
    attempted = 0
    excluded_ids: set[uuid.UUID] = set()

    while attempted < batch_size:
        query = select(
            Task.id, Task.organization_id, Task.schedule_interval_seconds, Task.next_run_at
        ).where(
            Task.schedule_interval_seconds.is_not(None),
            Task.is_active.is_(True),
            Task.next_run_at <= now,
        )
        if excluded_ids:
            query = query.where(Task.id.not_in(list(excluded_ids)))
        query = query.order_by(Task.next_run_at.asc(), Task.id.asc()).limit(1)
        if _supports_skip_locked(db):
            query = query.with_for_update(skip_locked=True, of=Task)
        else:  # pragma: no cover - sandbox-only fallback, see engine.py's own docstring
            query = query.with_for_update()

        row = db.execute(query).first()
        if row is None:
            db.commit()  # release lock state; no-op if nothing was locked
            break

        attempted += 1
        task_id, organization_id, interval_seconds, old_next_run_at = row

        try:
            new_next_run_at = now + timedelta(seconds=interval_seconds)
            result = db.execute(
                update(Task)
                .where(
                    Task.id == task_id,
                    Task.next_run_at == old_next_run_at,
                    Task.is_active.is_(True),
                    Task.schedule_interval_seconds.is_not(None),
                )
                .values(next_run_at=new_next_run_at)
            )
            if result.rowcount != 1:
                # Lost the race (should be impossible under SKIP LOCKED,
                # but the guard makes it safe regardless) -- try a
                # different candidate for the rest of this pass.
                db.rollback()
                excluded_ids.add(task_id)
                continue

            run = create_task_run_record(
                db,
                organization_id=organization_id,
                task_id=task_id,
                triggered_by=None,
                source_task_run_id=None,
            )
            db.commit()
            created += 1
            logger.info(
                "Scheduler created TaskRun %s for Task %s (org %s): "
                "next_run_at %s -> %s",
                run.id, task_id, organization_id, old_next_run_at, new_next_run_at,
            )
        except Exception:  # noqa: BLE001 - one task's failure must not abort the pass
            db.rollback()
            metrics.scheduler_errors_total.inc()
            logger.exception(
                "Scheduler failed to process due Task %s (org %s); rolled back and "
                "excluded it for the remainder of this pass",
                task_id, organization_id,
            )
            excluded_ids.add(task_id)
            continue

    metrics.scheduler_runs_created_total.inc(created)
    metrics.scheduler_last_success_timestamp_seconds.set(time.time())
    if created:
        logger.info("Scheduler pass (%s) created %d TaskRun(s)", worker_id, created)
    return created
