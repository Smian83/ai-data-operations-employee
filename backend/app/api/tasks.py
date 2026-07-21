"""
Task CRUD + TaskRun sub-resource, tenant-scoped.

Inactive resources behave exactly like non-existent ones (404) everywhere,
including when referenced by a different resource: a Task pointing at an
inactive DataSource, or a run requested against an inactive Task, both 404
rather than a distinct "conflict" status — per explicit product decision,
so inactive-resource behavior is uniform across the whole API.
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import PaginationParams, get_current_active_user
from app.db.session import get_db
from app.models.cleaning_change import CleaningChange
from app.models.cleaning_run import CleaningRun
from app.models.data_profile import DataProfile
from app.models.data_source import DataSource
from app.models.enums import TaskType
from app.models.task import Task
from app.models.task_run import TaskRun
from app.models.task_run_event import TaskRunEvent
from app.models.user import User
from app.schemas.cleaning_change import CleaningChangeRead
from app.schemas.cleaning_run import CleaningRunRead
from app.schemas.data_profile import DataProfileRead
from app.schemas.pagination import PaginatedResponse
from app.schemas.task import TaskCreate, TaskRead, TaskUpdate
from app.schemas.task_run import TaskRunCreate, TaskRunRead
from app.schemas.task_run_event import TaskRunEventRead

router = APIRouter(prefix="/tasks", tags=["tasks"])


def _name_taken(db: Session, org_id: uuid.UUID, name: str, exclude_id: uuid.UUID | None = None) -> bool:
    stmt = select(Task.id).where(
        Task.organization_id == org_id,
        func.lower(func.trim(Task.name)) == name.strip().lower(),
        Task.is_active.is_(True),
    )
    if exclude_id is not None:
        stmt = stmt.where(Task.id != exclude_id)
    return db.execute(stmt).scalar_one_or_none() is not None


def _get_active_task_or_404(db: Session, task_id: uuid.UUID, org_id: uuid.UUID) -> Task:
    task = db.execute(
        select(Task).where(
            Task.id == task_id,
            Task.organization_id == org_id,
            Task.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if task is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found")
    return task


def _validate_data_source_ref(
    db: Session, data_source_id: uuid.UUID | None, org_id: uuid.UUID
) -> None:
    """A Task's data_source_id, if set, must reference an ACTIVE DataSource
    in the SAME organization. Missing, cross-org, or inactive all 404 —
    inactive resources are indistinguishable from non-existent ones."""
    if data_source_id is None:
        return
    exists = db.execute(
        select(DataSource.id).where(
            DataSource.id == data_source_id,
            DataSource.organization_id == org_id,
            DataSource.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if exists is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Data source not found",
        )


@router.post("", response_model=TaskRead, status_code=status.HTTP_201_CREATED)
def create_task(
    payload: TaskCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Task:
    _validate_data_source_ref(db, payload.data_source_id, current_user.organization_id)

    if _name_taken(db, current_user.organization_id, payload.name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A task named '{payload.name}' already exists",
        )

    task = Task(
        organization_id=current_user.organization_id,
        data_source_id=payload.data_source_id,
        name=payload.name,
        description=payload.description,
        task_type=payload.task_type,
        schedule=payload.schedule,
        max_attempts=payload.max_attempts,
        timeout_seconds=payload.timeout_seconds,
        created_by=current_user.id,
    )
    db.add(task)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A task named '{payload.name}' already exists",
        )
    db.refresh(task)
    return task


@router.get("", response_model=PaginatedResponse[TaskRead])
def list_tasks(
    pagination: PaginationParams = Depends(),
    include_inactive: bool = Query(default=False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> PaginatedResponse[TaskRead]:
    filters = [Task.organization_id == current_user.organization_id]
    if not include_inactive:
        filters.append(Task.is_active.is_(True))

    total = db.execute(select(func.count()).select_from(Task).where(*filters)).scalar_one()
    rows = db.execute(
        select(Task)
        .where(*filters)
        .order_by(Task.created_at.desc())
        .limit(pagination.limit)
        .offset(pagination.offset)
    ).scalars().all()

    return PaginatedResponse(
        items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset
    )


@router.get("/{task_id}", response_model=TaskRead)
def get_task(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Task:
    return _get_active_task_or_404(db, task_id, current_user.organization_id)


@router.patch("/{task_id}", response_model=TaskRead)
def update_task(
    task_id: uuid.UUID,
    payload: TaskUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> Task:
    task = _get_active_task_or_404(db, task_id, current_user.organization_id)

    if payload.name is not None and payload.name.lower() != task.name.strip().lower():
        if _name_taken(db, current_user.organization_id, payload.name, exclude_id=task.id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A task named '{payload.name}' already exists",
            )
        task.name = payload.name

    if "data_source_id" in payload.model_fields_set:
        _validate_data_source_ref(db, payload.data_source_id, current_user.organization_id)
        task.data_source_id = payload.data_source_id
    if payload.description is not None:
        task.description = payload.description
    if payload.task_type is not None:
        task.task_type = payload.task_type
    if "schedule" in payload.model_fields_set:
        task.schedule = payload.schedule
    if "max_attempts" in payload.model_fields_set:
        task.max_attempts = payload.max_attempts
    if "timeout_seconds" in payload.model_fields_set:
        task.timeout_seconds = payload.timeout_seconds

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A task named '{payload.name}' already exists",
        )
    db.refresh(task)
    return task


@router.delete("/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(
    task_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> None:
    task = _get_active_task_or_404(db, task_id, current_user.organization_id)
    task.is_active = False
    db.commit()


# --- Task Runs (sub-resource) -------------------------------------------------


@router.post("/{task_id}/runs", response_model=TaskRunRead, status_code=status.HTTP_201_CREATED)
def create_task_run(
    task_id: uuid.UUID,
    payload: TaskRunCreate | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> TaskRun:
    """Module 6: payload is optional -- omitted entirely, it behaves exactly
    as before. source_task_run_id is required for TRANSFORM tasks (which
    prior SYNC run's DataProfile to clean) and rejected for every other
    task type, so the field's meaning can never be ambiguous per task."""
    # Inactive or cross-org task -> 404, same as any other direct access.
    task = _get_active_task_or_404(db, task_id, current_user.organization_id)

    source_task_run_id = payload.source_task_run_id if payload is not None else None

    if task.task_type == TaskType.TRANSFORM:
        if source_task_run_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="source_task_run_id is required for TRANSFORM tasks",
            )
        source_run_exists = db.execute(
            select(TaskRun.id).where(
                TaskRun.id == source_task_run_id,
                TaskRun.organization_id == current_user.organization_id,
            )
        ).scalar_one_or_none()
        if source_run_exists is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Source task run not found",
            )
    elif source_task_run_id is not None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="source_task_run_id is only valid for TRANSFORM tasks",
        )

    run = TaskRun(
        organization_id=current_user.organization_id,
        task_id=task.id,
        triggered_by=current_user.id,
        source_task_run_id=source_task_run_id,
        # status defaults to PENDING at the model layer; started_at/
        # finished_at/error_message all remain NULL, satisfying
        # ck_task_runs_status_invariants for the 'pending' branch.
    )
    db.add(run)
    db.commit()
    db.refresh(run)
    return run


@router.get("/{task_id}/runs", response_model=PaginatedResponse[TaskRunRead])
def list_task_runs(
    task_id: uuid.UUID,
    pagination: PaginationParams = Depends(),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> PaginatedResponse[TaskRunRead]:
    # Confirms the task itself is visible to this org (404 otherwise) before
    # listing its runs — same inactive/cross-org rules as everything else.
    task = _get_active_task_or_404(db, task_id, current_user.organization_id)

    filters = [
        TaskRun.task_id == task.id,
        TaskRun.organization_id == current_user.organization_id,
    ]
    total = db.execute(select(func.count()).select_from(TaskRun).where(*filters)).scalar_one()
    rows = db.execute(
        select(TaskRun)
        .where(*filters)
        .order_by(TaskRun.created_at.desc())
        .limit(pagination.limit)
        .offset(pagination.offset)
    ).scalars().all()

    return PaginatedResponse(
        items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset
    )


@router.get("/{task_id}/runs/{run_id}", response_model=TaskRunRead)
def get_task_run(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> TaskRun:
    task = _get_active_task_or_404(db, task_id, current_user.organization_id)
    run = db.execute(
        select(TaskRun).where(
            TaskRun.id == run_id,
            TaskRun.task_id == task.id,
            TaskRun.organization_id == current_user.organization_id,
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task run not found")
    return run


@router.get("/{task_id}/runs/{run_id}/profile", response_model=DataProfileRead)
def get_task_run_profile(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> DataProfile:
    """Module 5: the immutable CSV profiling result for one TaskRun, if the
    execution engine has produced one. 404 if the run itself isn't visible
    to this org, or if no profile exists yet (e.g. the run hasn't completed,
    or wasn't a CSV_UPLOAD sync) -- same inactive/cross-org/not-found
    uniformity as every other endpoint in this router."""
    task = _get_active_task_or_404(db, task_id, current_user.organization_id)
    run_exists = db.execute(
        select(TaskRun.id).where(
            TaskRun.id == run_id,
            TaskRun.task_id == task.id,
            TaskRun.organization_id == current_user.organization_id,
        )
    ).scalar_one_or_none()
    if run_exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task run not found")

    profile = db.execute(
        select(DataProfile).where(
            DataProfile.task_run_id == run_id,
            DataProfile.task_id == task.id,
            DataProfile.organization_id == current_user.organization_id,
        )
    ).scalar_one_or_none()
    if profile is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Data profile not found")
    return profile


@router.get("/{task_id}/runs/{run_id}/events", response_model=PaginatedResponse[TaskRunEventRead])
def list_task_run_events(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    pagination: PaginationParams = Depends(),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> PaginatedResponse[TaskRunEventRead]:
    """Read-only Module 4 audit trail for a single TaskRun: every claim,
    heartbeat-driven requeue, success, failure, and reaper recovery, in
    order. Never writable via the API -- only the execution engine appends
    to this table."""
    run = db.execute(
        select(TaskRun.id).where(
            TaskRun.id == run_id,
            TaskRun.task_id == task_id,
            TaskRun.organization_id == current_user.organization_id,
        )
    ).scalar_one_or_none()
    if run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task run not found")

    filters = [
        TaskRunEvent.task_run_id == run_id,
        TaskRunEvent.organization_id == current_user.organization_id,
    ]
    total = db.execute(select(func.count()).select_from(TaskRunEvent).where(*filters)).scalar_one()
    rows = db.execute(
        select(TaskRunEvent)
        .where(*filters)
        .order_by(TaskRunEvent.created_at)
        .limit(pagination.limit)
        .offset(pagination.offset)
    ).scalars().all()

    return PaginatedResponse(
        items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset
    )

# --- Cleaning results (Module 6 sub-resource) --------------------------------


def _get_cleaning_run_or_404(
    db: Session, task_id: uuid.UUID, run_id: uuid.UUID, org_id: uuid.UUID
) -> CleaningRun:
    """Shared 404 chain for every cleaning-result endpoint: task visible ->
    run visible -> cleaning result exists. Same inactive/cross-org/not-found
    uniformity as get_task_run_profile."""
    task = _get_active_task_or_404(db, task_id, org_id)
    run_exists = db.execute(
        select(TaskRun.id).where(
            TaskRun.id == run_id,
            TaskRun.task_id == task.id,
            TaskRun.organization_id == org_id,
        )
    ).scalar_one_or_none()
    if run_exists is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task run not found")

    cleaning_run = db.execute(
        select(CleaningRun).where(
            CleaningRun.task_run_id == run_id,
            CleaningRun.task_id == task.id,
            CleaningRun.organization_id == org_id,
        )
    ).scalar_one_or_none()
    if cleaning_run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Cleaning result not found"
        )
    return cleaning_run


@router.get("/{task_id}/runs/{run_id}/cleaning", response_model=CleaningRunRead)
def get_task_run_cleaning(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> CleaningRun:
    """Module 6: the summary result of a cleaning TaskRun -- counts,
    confidence, output location/hash, and current approval status. 404 if
    the run isn't visible to this org, or no cleaning result exists yet
    (e.g. the run hasn't completed, or wasn't a TRANSFORM run)."""
    return _get_cleaning_run_or_404(db, task_id, run_id, current_user.organization_id)


@router.get(
    "/{task_id}/runs/{run_id}/cleaning/changes",
    response_model=PaginatedResponse[CleaningChangeRead],
)
def list_task_run_cleaning_changes(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    pagination: PaginationParams = Depends(),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> PaginatedResponse[CleaningChangeRead]:
    """Module 6: the bounded per-cell change log for a cleaning run, in row
    order -- same pagination shape as list_task_run_events. Note this may
    under-represent total_changes_count on CleaningRun for a run whose
    change volume exceeded CLEANING_MAX_PERSISTED_CHANGES; the aggregate
    count on the parent CleaningRun is always accurate even when the
    per-change rows are capped."""
    cleaning_run = _get_cleaning_run_or_404(db, task_id, run_id, current_user.organization_id)

    filters = [
        CleaningChange.cleaning_run_id == cleaning_run.id,
        CleaningChange.organization_id == current_user.organization_id,
    ]
    total = db.execute(select(func.count()).select_from(CleaningChange).where(*filters)).scalar_one()
    rows = db.execute(
        select(CleaningChange)
        .where(*filters)
        .order_by(CleaningChange.row_index)
        .limit(pagination.limit)
        .offset(pagination.offset)
    ).scalars().all()

    return PaginatedResponse(
        items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset
    )


@router.post("/{task_id}/runs/{run_id}/cleaning/approve", response_model=CleaningRunRead)
def approve_task_run_cleaning(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> CleaningRun:
    """Module 6 approval state machine: pending_review -> approved only.
    Any other starting status is a 409 conflict, not a 400 -- the request
    is well-formed, the resource is simply not in a state that accepts it."""
    cleaning_run = _get_cleaning_run_or_404(db, task_id, run_id, current_user.organization_id)
    if cleaning_run.status != "pending_review":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot approve a cleaning run with status '{cleaning_run.status}'",
        )
    cleaning_run.status = "approved"
    cleaning_run.approved_by = current_user.id
    cleaning_run.approved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(cleaning_run)
    return cleaning_run


@router.post("/{task_id}/runs/{run_id}/cleaning/reject", response_model=CleaningRunRead)
def reject_task_run_cleaning(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> CleaningRun:
    """Module 6 approval state machine: pending_review -> rejected only."""
    cleaning_run = _get_cleaning_run_or_404(db, task_id, run_id, current_user.organization_id)
    if cleaning_run.status != "pending_review":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot reject a cleaning run with status '{cleaning_run.status}'",
        )
    cleaning_run.status = "rejected"
    cleaning_run.rejected_by = current_user.id
    cleaning_run.rejected_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(cleaning_run)
    return cleaning_run


@router.post("/{task_id}/runs/{run_id}/cleaning/rollback", response_model=CleaningRunRead)
def rollback_task_run_cleaning(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> CleaningRun:
    """Module 6 approval state machine: approved -> rolled_back only (a
    rejected or already-rolled-back run cannot be rolled back). A pure
    status transition -- the output file and every CleaningChange row are
    untouched, per the design doc's non-destructive rollback guarantee."""
    cleaning_run = _get_cleaning_run_or_404(db, task_id, run_id, current_user.organization_id)
    if cleaning_run.status != "approved":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot roll back a cleaning run with status '{cleaning_run.status}'",
        )
    cleaning_run.status = "rolled_back"
    cleaning_run.rolled_back_by = current_user.id
    cleaning_run.rolled_back_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(cleaning_run)
    return cleaning_run
