"""Generic TaskRun row construction, shared by every creator of a TaskRun.

Module 12 introduces a second caller (the scheduler, app/worker/scheduler.py)
alongside the original manual path (POST /tasks/{id}/runs,
app/api/tasks.py::create_task_run). Rather than duplicate the five-keyword-
argument construction in two places -- where it could silently drift apart
-- both paths call the single function below.

This module deliberately owns exactly one thing: constructing a TaskRun
instance with the correct keyword arguments. It does NOT commit or flush
the session (the caller's own transaction boundary decides that), does NOT
perform business-rule validation (source_task_run_id's task-type-dependent
required/forbidden rules stay in app/api/tasks.py, exactly as before this
module; the scheduler's caller already guarantees SYNC-only and
source_task_run_id=None by construction of its own WHERE clause -- see
app/worker/scheduler.py), does NOT set `status` or `idempotency_key` (left
to the model's own defaults, unchanged), contains no HTTP logic, and
performs no authentication. It is intentionally the smallest possible
shared helper, not a broader service layer.
"""
import uuid

from sqlalchemy.orm import Session

from app.models.task_run import TaskRun


def create_task_run_record(
    db: Session,
    *,
    organization_id: uuid.UUID,
    task_id: uuid.UUID,
    triggered_by: uuid.UUID | None,
    source_task_run_id: uuid.UUID | None,
) -> TaskRun:
    """Construct and `db.add()` a new TaskRun. Does not commit or flush --
    the caller's own transaction (a request/response cycle for the manual
    API path, a single-task claim transaction for the scheduler path)
    decides when that happens. `status` defaults to PENDING and
    started_at/finished_at/error_message all remain NULL at the model
    layer, satisfying ck_task_runs_status_invariants' 'pending' branch."""
    run = TaskRun(
        organization_id=organization_id,
        task_id=task_id,
        triggered_by=triggered_by,
        source_task_run_id=source_task_run_id,
    )
    db.add(run)
    return run
