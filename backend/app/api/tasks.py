"""
Task CRUD + TaskRun sub-resource, tenant-scoped.

Inactive resources behave exactly like non-existent ones (404) everywhere,
including when referenced by a different resource: a Task pointing at an
inactive DataSource, or a run requested against an inactive Task, both 404
rather than a distinct "conflict" status — per explicit product decision,
so inactive-resource behavior is uniform across the whole API.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import PaginationParams, get_current_active_user
from app.db.session import get_db
from app.models.data_profile import DataProfile
from app.models.data_source import DataSource
from app.models.task import Task
from app.models.task_run import TaskRun
from app.models.task_run_event import TaskRunEvent
from app.models.user import User
from app.schemas.data_profile import DataProfileRead
from app.schemas.pagination import PaginatedResponse
from app.schemas.task import TaskCreate, TaskRead, TaskUpdate
from app.schemas.task_run import TaskRunRead
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
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> TaskRun:
    # Inactive or cross-org task -> 404, same as any other direct access.
    task = _get_active_task_or_404(db, task_id, current_user.organization_id)

    run = TaskRun(
        organization_id=current_user.organization_id,
        task_id=task.id,
        triggered_by=current_user.id,
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
