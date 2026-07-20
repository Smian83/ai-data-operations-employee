"""
Reaper: recovers TaskRuns whose lease expired without a heartbeat --
almost always because the worker that claimed them crashed, was killed, or
lost network connectivity. Runs as a periodic loop, independent of the
claiming workers.

Reclaiming uses the exact same atomic, guarded-UPDATE pattern as the
engine's claim/complete paths (WHERE status='running' AND lease_expires_at
< now(), same SKIP LOCKED behavior on Postgres), so a race between the
reaper and a worker that heartbeats at the last possible moment can never
double-reclaim a row -- one of the two UPDATEs simply affects zero rows.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.enums import TaskRunStatus
from app.models.task_run import TaskRun
from app.worker.engine import _pending_count, _supports_skip_locked
from app.worker import metrics

logger = logging.getLogger(__name__)


def reap_expired_runs(db: Session, worker_id: str = "reaper") -> int:
    """Find every 'running' TaskRun whose lease has expired and either
    requeue it for retry or terminate it as failed, exactly as
    engine.complete_failure() would for a worker-reported failure --
    except the failure reason is "lease expired / worker lost", and the
    event log records it as reaper-driven, not worker-reported."""
    from app.worker.engine import complete_failure  # local import: avoid a cycle

    now = datetime.now(timezone.utc)
    query = select(TaskRun.id, TaskRun.lease_token).where(
        TaskRun.status == TaskRunStatus.RUNNING,
        TaskRun.lease_expires_at < now,
    )
    if _supports_skip_locked(db):
        query = query.with_for_update(skip_locked=True, of=TaskRun)
    else:  # pragma: no cover - sandbox-only fallback
        query = query.with_for_update()

    expired = db.execute(query).all()
    db.commit()  # release the row lock before calling complete_failure, which opens its own updates

    recovered = 0
    for task_run_id, lease_token in expired:
        try:
            complete_failure(
                db,
                task_run_id=task_run_id,
                lease_token=lease_token,
                worker_id=worker_id,
                error_message="Execution timed out: lease expired without a heartbeat "
                "(worker likely crashed or lost connectivity).",
                retryable=True,
                log_output=None,
            )
            recovered += 1
        except Exception:  # noqa: BLE001 - a single row's failure must not abort the sweep
            db.rollback()
            logger.exception("Reaper failed to recover TaskRun %s", task_run_id)

    if recovered:
        logger.info("Reaper recovered %d stuck TaskRun(s)", recovered)
    metrics.queue_depth.set(_pending_count(db))
    return recovered
