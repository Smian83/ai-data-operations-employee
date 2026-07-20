"""
The core execution engine: claiming pending TaskRuns, heartbeating,
completing, and requeuing them for retry.

Concurrency safety comes from two layers, both required:

1. `SELECT ... FOR UPDATE SKIP LOCKED` at claim time -- a PostgreSQL-only
   feature. Concurrent workers polling simultaneously never block each
   other and never see a row another transaction already has locked, so
   two workers can never claim the same pending row. On SQLite (sandbox
   only -- SQLite has no row-level locking) this degenerates to a plain
   SELECT; see `_supports_skip_locked()` below. This is a deliberate,
   explicitly-flagged sandbox fidelity gap -- true claim-concurrency
   safety can only be verified against real PostgreSQL, never SQLite.
2. A `lease_token` fencing token, generated fresh on every claim (including
   every retry-requeue-then-reclaim cycle). Every heartbeat and every
   completion call must present the lease_token it was given at claim time,
   and the guarded UPDATE's WHERE clause checks `lease_token = :token AND
   status = 'running'`. If a worker's lease already expired and the reaper
   (or another worker) reclaimed the row, that row now has a *different*
   lease_token -- so the original worker's late heartbeat/completion
   affects zero rows instead of corrupting state it no longer owns.
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models.enums import TaskRunStatus
from app.models.task import Task
from app.models.task_run import TaskRun
from app.models.task_run_event import TaskRunEvent
from app.worker import metrics

logger = logging.getLogger(__name__)


class LeaseLostError(Exception):
    """Raised when a heartbeat or completion call no longer owns the lease
    it was given (reclaimed by the reaper or another worker). Callers must
    treat this as "stop executing immediately" -- the row is no longer
    theirs and continuing risks a duplicate/conflicting effect."""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware(dt: datetime | None) -> datetime | None:
    """SQLite (sandbox only -- PostgreSQL always round-trips tzinfo
    correctly) silently drops tzinfo on DateTime(timezone=True) columns
    when reading rows back, even though it was written as UTC-aware. Any
    datetime pulled from the DB and compared/subtracted against a fresh
    datetime.now(timezone.utc) must be normalized through this first, or
    the comparison raises TypeError on SQLite while working fine on
    PostgreSQL -- exactly the kind of sandbox/production divergence this
    project has flagged and closed in every prior module."""
    if dt is not None and dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _supports_skip_locked(db: Session) -> bool:
    return db.get_bind().dialect.name == "postgresql"


def _record_event(
    db: Session,
    task_run: TaskRun,
    event_type: str,
    from_status: str | None,
    to_status: str | None,
    worker_id: str | None,
    detail: dict,
) -> None:
    db.add(
        TaskRunEvent(
            organization_id=task_run.organization_id,
            task_run_id=task_run.id,
            event_type=event_type,
            from_status=from_status,
            to_status=to_status,
            worker_id=worker_id,
            detail=detail,
        )
    )


def claim_batch(db: Session, worker_id: str, batch_size: int | None = None) -> list[TaskRun]:
    """Atomically claim up to `batch_size` pending, due TaskRuns for
    `worker_id`. Each claimed row gets a fresh lease_token, started_at,
    lease_expires_at, and an incremented attempt_count. Commits once for
    the whole batch."""
    settings = get_settings()
    if batch_size is None:
        batch_size = settings.worker_claim_batch_size
    now = _now()

    query = (
        select(TaskRun.id, Task.timeout_seconds)
        .join(Task, Task.id == TaskRun.task_id)
        .where(
            TaskRun.status == TaskRunStatus.PENDING,
            (TaskRun.next_retry_at.is_(None)) | (TaskRun.next_retry_at <= now),
        )
        .order_by(TaskRun.created_at)
        .limit(batch_size)
    )
    if _supports_skip_locked(db):
        query = query.with_for_update(skip_locked=True, of=TaskRun)
    else:  # pragma: no cover - sandbox-only fallback, see module docstring
        query = query.with_for_update()

    candidates = db.execute(query).all()
    claimed: list[TaskRun] = []
    for task_run_id, timeout_seconds in candidates:
        lease_token = uuid.uuid4()
        effective_timeout = timeout_seconds or settings.worker_default_timeout_seconds
        lease_expires_at = now + timedelta(seconds=effective_timeout)

        result = db.execute(
            update(TaskRun)
            .where(TaskRun.id == task_run_id, TaskRun.status == TaskRunStatus.PENDING)
            .values(
                status=TaskRunStatus.RUNNING,
                started_at=now,
                lease_token=lease_token,
                lease_expires_at=lease_expires_at,
                last_heartbeat_at=now,
                attempt_count=TaskRun.attempt_count + 1,
            )
        )
        if result.rowcount != 1:
            # Lost the race (should be impossible under SKIP LOCKED, but
            # the WHERE guard makes this safe regardless).
            continue

        task_run = db.get(TaskRun, task_run_id)
        _record_event(
            db, task_run, "claimed", "pending", "running", worker_id,
            {"attempt_count": task_run.attempt_count, "lease_expires_at": lease_expires_at.isoformat()},
        )
        claimed.append(task_run)

    db.commit()
    metrics.tasks_claimed_total.inc(len(claimed))
    metrics.queue_depth.set(_pending_count(db))
    return claimed


def heartbeat(db: Session, task_run_id: uuid.UUID, lease_token: uuid.UUID, worker_id: str) -> None:
    """Extend a claimed run's lease. Raises LeaseLostError if the caller no
    longer owns the lease."""
    settings = get_settings()
    task_run = db.get(TaskRun, task_run_id)
    timeout_seconds = task_run.task.timeout_seconds if task_run else None
    effective_timeout = timeout_seconds or settings.worker_default_timeout_seconds
    now = _now()
    new_expiry = now + timedelta(seconds=effective_timeout)

    result = db.execute(
        update(TaskRun)
        .where(
            TaskRun.id == task_run_id,
            TaskRun.lease_token == lease_token,
            TaskRun.status == TaskRunStatus.RUNNING,
        )
        .values(last_heartbeat_at=now, lease_expires_at=new_expiry)
    )
    if result.rowcount != 1:
        db.rollback()
        raise LeaseLostError(f"Lease for TaskRun {task_run_id} is no longer held by this worker")
    db.commit()


def complete_success(
    db: Session,
    task_run_id: uuid.UUID,
    lease_token: uuid.UUID,
    worker_id: str,
    log_output: str | None = None,
) -> None:
    now = _now()
    result = db.execute(
        update(TaskRun)
        .where(
            TaskRun.id == task_run_id,
            TaskRun.lease_token == lease_token,
            TaskRun.status == TaskRunStatus.RUNNING,
        )
        .values(
            status=TaskRunStatus.SUCCESS,
            finished_at=now,
            log_output=log_output,
            lease_token=None,
            lease_expires_at=None,
        )
    )
    if result.rowcount != 1:
        db.rollback()
        raise LeaseLostError(f"Lease for TaskRun {task_run_id} is no longer held by this worker")

    task_run = db.get(TaskRun, task_run_id)
    started_at = _ensure_aware(task_run.started_at)
    duration = (now - started_at).total_seconds() if started_at else None
    _record_event(db, task_run, "succeeded", "running", "success", worker_id, {"duration_seconds": duration})
    db.commit()
    metrics.tasks_completed_total.inc()
    if duration is not None:
        metrics.task_execution_duration_seconds.observe(duration)
    metrics.queue_depth.set(_pending_count(db))


def complete_failure(
    db: Session,
    task_run_id: uuid.UUID,
    lease_token: uuid.UUID,
    worker_id: str,
    error_message: str,
    retryable: bool,
    log_output: str | None = None,
) -> None:
    """Report a failed execution attempt. If `retryable` and attempts
    remain, requeues to 'pending' (resetting started_at/finished_at/
    error_message to NULL, per Module 3's unmodified CHECK constraint) with
    backoff. Otherwise terminates the run as 'failed'."""
    settings = get_settings()
    now = _now()
    task_run = db.get(TaskRun, task_run_id)
    if task_run is None or task_run.lease_token != lease_token or task_run.status != TaskRunStatus.RUNNING:
        raise LeaseLostError(f"Lease for TaskRun {task_run_id} is no longer held by this worker")

    max_attempts = task_run.task.max_attempts or settings.worker_default_max_attempts
    should_retry = retryable and task_run.attempt_count < max_attempts

    if should_retry:
        delay = min(
            settings.worker_retry_base_delay_seconds * (2 ** (task_run.attempt_count - 1)),
            settings.worker_retry_max_delay_seconds,
        )
        next_retry_at = now + timedelta(seconds=delay)
        result = db.execute(
            update(TaskRun)
            .where(
                TaskRun.id == task_run_id,
                TaskRun.lease_token == lease_token,
                TaskRun.status == TaskRunStatus.RUNNING,
            )
            .values(
                status=TaskRunStatus.PENDING,
                started_at=None,
                finished_at=None,
                error_message=None,
                log_output=log_output,
                lease_token=None,
                lease_expires_at=None,
                next_retry_at=next_retry_at,
            )
        )
        event_type, to_status = "requeued", "pending"
        detail = {"error_message": error_message, "next_retry_at": next_retry_at.isoformat(), "attempt_count": task_run.attempt_count}
        metrics.tasks_retried_total.inc()
    else:
        result = db.execute(
            update(TaskRun)
            .where(
                TaskRun.id == task_run_id,
                TaskRun.lease_token == lease_token,
                TaskRun.status == TaskRunStatus.RUNNING,
            )
            .values(
                status=TaskRunStatus.FAILED,
                finished_at=now,
                error_message=error_message,
                log_output=log_output,
                lease_token=None,
                lease_expires_at=None,
            )
        )
        event_type, to_status = "failed", "failed"
        detail = {"error_message": error_message, "attempt_count": task_run.attempt_count, "retryable": retryable}
        metrics.tasks_failed_total.inc()

    if result.rowcount != 1:
        db.rollback()
        raise LeaseLostError(f"Lease for TaskRun {task_run_id} is no longer held by this worker")

    _record_event(db, task_run, event_type, "running", to_status, worker_id, detail)
    db.commit()
    metrics.queue_depth.set(_pending_count(db))


def _pending_count(db: Session) -> int:
    from sqlalchemy import func as sa_func
    return db.execute(
        select(sa_func.count()).select_from(TaskRun).where(TaskRun.status == TaskRunStatus.PENDING)
    ).scalar_one()
