"""
Worker process entrypoint: the loop that claims TaskRuns, executes them via
the handler registry, and reports success/failure back to the engine.

Runs as its own OS process (`python -m app.worker`), separate from the
FastAPI app -- a worker crash never affects API availability, and the two
scale independently. The reaper runs as a second loop in the same process
for the MVP; splitting it into its own process/service is a trivial future
change since it only depends on a DB session.
"""
import logging
import socket
import threading
import time
import uuid

from app.core.logging import configure_logging
from app.db.session import SessionLocal
from app.worker.credentials import DatabaseCredentialProvider
from app.worker.engine import LeaseLostError, claim_batch, complete_failure, complete_success, heartbeat
from app.worker.handlers import PermanentHandlerLookupError, get_handler
from app.worker.handlers.base import ExecutionContext
from app.worker.reaper import reap_expired_runs
from app.worker.retention import purge_expired_artifacts
from app.worker.scheduler import run_due_schedules
from app.core.config import get_settings
from app.worker.handlers.base import PermanentExecutionError, TransientExecutionError

logger = logging.getLogger(__name__)


def _worker_id() -> str:
    settings = get_settings()
    return f"{settings.worker_id}@{socket.gethostname()}:{uuid.uuid4().hex[:8]}"


def execute_one(db, task_run, worker_id: str) -> None:
    """Execute a single already-claimed TaskRun and report the result.
    A background heartbeat keeps the lease alive while the handler runs;
    LeaseLostError propagating out of the heartbeat thread means the run
    was reclaimed elsewhere and execution must stop reporting results."""
    lease_token = task_run.lease_token
    task_run_id = task_run.id
    task = task_run.task
    data_source = task.data_source

    stop_heartbeat = threading.Event()

    def _heartbeat_loop() -> None:
        settings = get_settings()
        hb_db = SessionLocal()
        try:
            while not stop_heartbeat.wait(settings.worker_heartbeat_interval_seconds):
                try:
                    heartbeat(hb_db, task_run_id, lease_token, worker_id)
                except LeaseLostError:
                    logger.warning("Lease lost for TaskRun %s during heartbeat", task_run_id)
                    return
        finally:
            hb_db.close()

    hb_thread = threading.Thread(target=_heartbeat_loop, daemon=True)
    hb_thread.start()

    try:
        handler = get_handler(task.task_type)
        context = ExecutionContext(
            task_run=task_run,
            task=task,
            data_source=data_source,
            idempotency_key=str(task_run.idempotency_key),
            credential_provider=DatabaseCredentialProvider(db),
        )
        log_output = handler.execute(context)
        complete_success(db, task_run_id, lease_token, worker_id, log_output=log_output)
    except TransientExecutionError as exc:
        complete_failure(db, task_run_id, lease_token, worker_id, str(exc), retryable=True)
    except (PermanentExecutionError, PermanentHandlerLookupError) as exc:
        complete_failure(db, task_run_id, lease_token, worker_id, str(exc), retryable=False)
    except LeaseLostError:
        logger.warning("Lease lost for TaskRun %s before result could be reported", task_run_id)
    except Exception as exc:  # noqa: BLE001 - unexpected errors are treated as retryable
        logger.exception("Unexpected error executing TaskRun %s", task_run_id)
        try:
            complete_failure(db, task_run_id, lease_token, worker_id, f"Unexpected error: {exc}", retryable=True)
        except LeaseLostError:
            pass
    finally:
        stop_heartbeat.set()
        hb_thread.join(timeout=5)


def run_forever() -> None:  # pragma: no cover - exercised via execute_one/claim_batch in tests
    configure_logging()
    settings = get_settings()
    worker_id = _worker_id()
    logger.info("Worker %s starting", worker_id)
    last_reap = 0.0
    last_schedule = 0.0
    last_retention = 0.0

    while True:
        db = SessionLocal()
        try:
            # Module 12: runs BEFORE claim_batch (not after) so a TaskRun
            # the scheduler creates this iteration can be picked up by
            # THIS SAME iteration's claim_batch call below, rather than
            # waiting a full worker_poll_interval_seconds for the next
            # loop. Wrapped defensively: an isolated scheduling failure
            # (e.g. a transient DB error on the initial due-task SELECT,
            # outside any single task's own per-task try/except in
            # run_due_schedules) must never crash the whole worker process
            # -- claiming and executing already-pending TaskRuns, and
            # reaping, must continue regardless.
            now = time.monotonic()
            if now - last_schedule >= settings.scheduler_poll_interval_seconds:
                try:
                    run_due_schedules(db, worker_id="scheduler", batch_size=settings.scheduler_claim_batch_size)
                except Exception:  # noqa: BLE001 - see comment above
                    db.rollback()
                    logger.exception("Scheduler pass failed unexpectedly")
                last_schedule = now

            claimed = claim_batch(db, worker_id)
            for task_run in claimed:
                execute_one(db, task_run, worker_id)

            if now - last_reap >= settings.reaper_poll_interval_seconds:
                reap_expired_runs(db, worker_id="reaper")
                last_reap = now

            # Module 13: a third, independent background pass, on its own
            # timer -- unlike the scheduler above, retention has no
            # same-iteration ordering dependency on claim_batch (it never
            # produces a TaskRun claim_batch could pick up), so its
            # placement relative to claim_batch/execute_one does not
            # matter the way the scheduler's does. Wrapped in the same
            # defensive try/except as the scheduler pass above: an
            # isolated retention-pass failure must never crash the worker
            # process or block claim_batch/execute_one/reap_expired_runs
            # from running in this same iteration.
            # purge_expired_artifacts() already returns an all-zero result
            # without touching any row when settings.output_retention_enabled
            # is false, so no redundant enabled-check is added here.
            if now - last_retention >= settings.retention_poll_interval_seconds:
                try:
                    purge_expired_artifacts(
                        db,
                        batch_size=settings.retention_claim_batch_size,
                        dry_run=settings.output_retention_dry_run,
                    )
                except Exception:  # noqa: BLE001 - see scheduler comment above
                    db.rollback()
                    logger.exception("Retention pass failed unexpectedly")
                last_retention = now

            if not claimed:
                time.sleep(settings.worker_poll_interval_seconds)
        finally:
            db.close()


if __name__ == "__main__":  # pragma: no cover
    run_forever()
