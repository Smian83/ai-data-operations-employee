"""
Task CRUD + TaskRun sub-resource, tenant-scoped.

Inactive resources behave exactly like non-existent ones (404) everywhere,
including when referenced by a different resource: a Task pointing at an
inactive DataSource, or a run requested against an inactive Task, both 404
rather than a distinct "conflict" status — per explicit product decision,
so inactive-resource behavior is uniform across the whole API.
"""
import logging
import os
import uuid
from collections.abc import Iterator
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import StreamingResponse
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import PaginationParams, get_current_active_user
from app.artifacts.download import (
    ArtifactIntegrityError,
    ArtifactMissingError,
    ArtifactPathError,
    iter_artifact_chunks,
    open_verified_artifact,
    resolve_artifact_path,
    safe_download_filename,
)
from app.core.config import get_settings
from app.db.session import SessionLocal, get_db
from app.models.artifact_download_event import ArtifactDownloadEvent
from app.models.cleaning_change import CleaningChange
from app.models.cleaning_run import CleaningRun
from app.models.data_profile import DataProfile
from app.models.data_source import DataSource
from app.models.enums import TaskType
from app.models.export_row_exclusion import ExportRowExclusion
from app.models.export_run import ExportRun
from app.models.match_decision import MatchDecision
from app.models.match_group import MatchGroup
from app.models.match_rule_field import MatchRuleField
from app.models.match_rule_set import MatchRuleSet
from app.models.match_run import MatchRun
from app.models.match_skipped_block import MatchSkippedBlock
from app.models.standardization_change import StandardizationChange
from app.models.standardization_column_mapping import StandardizationColumnMapping
from app.models.standardization_lookup_entry import StandardizationLookupEntry
from app.models.standardization_run import StandardizationRun
from app.models.task import Task
from app.models.task_run import TaskRun
from app.models.task_run_event import TaskRunEvent
from app.models.user import User
from app.services.task_run_factory import create_task_run_record
from app.schemas.cleaning_change import CleaningChangeRead
from app.schemas.cleaning_run import CleaningRunRead
from app.schemas.data_profile import DataProfileRead
from app.schemas.export_row_exclusion import ExportRowExclusionRead
from app.schemas.export_run import ExportRunRead
from app.schemas.match_decision import MatchDecisionRead
from app.schemas.match_group import MatchGroupRead
from app.schemas.match_rule_set import MatchRuleSetCreate, MatchRuleSetRead
from app.schemas.match_run import MatchRunRead
from app.schemas.match_skipped_block import MatchSkippedBlockRead
from app.schemas.pagination import PaginatedResponse
from app.schemas.standardization_change import StandardizationChangeRead
from app.schemas.standardization_column_mapping import (
    StandardizationColumnMappingCreate,
    StandardizationColumnMappingRead,
)
from app.schemas.standardization_lookup_entry import (
    StandardizationLookupEntryCreate,
    StandardizationLookupEntryRead,
)
from app.schemas.standardization_run import StandardizationRunRead
from app.schemas.task import TaskCreate, TaskRead, TaskUpdate
from app.schemas.task_run import TaskRunCreate, TaskRunRead
from app.schemas.task_run_event import TaskRunEventRead

logger = logging.getLogger(__name__)

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


def _validate_schedule_interval_task_type(
    task_type: TaskType, schedule_interval_seconds: int | None
) -> None:
    """schedule_interval_seconds is only meaningful for SYNC tasks -- a
    scheduled TRANSFORM/STANDARDIZE/MATCH/EXPORT would need a chaining
    decision (which prior run to build on) that scheduling alone can't
    resolve, and OTHER has no defined execution semantics at all. Same
    category of cross-field, task-type-dependent business rule as
    source_task_run_id's own validation in create_task_run below -- same
    400 status, same explicit-HTTPException style, not a Pydantic-level
    check, since it depends on another field's value."""
    if schedule_interval_seconds is not None and task_type != TaskType.SYNC:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="schedule_interval_seconds is only valid for SYNC tasks",
        )


def _compute_next_run_at(schedule_interval_seconds: int) -> datetime:
    """Always anchored to "now", never to any prior next_run_at -- see
    app/worker/scheduler.py's own module docstring for why this is also
    the missed-schedule catch-up rule, not just the initial-activation
    rule."""
    return datetime.now(timezone.utc) + timedelta(seconds=schedule_interval_seconds)


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

    _validate_schedule_interval_task_type(payload.task_type, payload.schedule_interval_seconds)

    task = Task(
        organization_id=current_user.organization_id,
        data_source_id=payload.data_source_id,
        name=payload.name,
        description=payload.description,
        task_type=payload.task_type,
        schedule=payload.schedule,
        schedule_interval_seconds=payload.schedule_interval_seconds,
        next_run_at=(
            _compute_next_run_at(payload.schedule_interval_seconds)
            if payload.schedule_interval_seconds is not None
            else None
        ),
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

    # schedule_interval_seconds: omitted from the request -> leave the
    # current schedule entirely unchanged (payload.model_fields_set, not
    # `is not None`, is what distinguishes "omitted" from "explicit null" --
    # same mechanism already used for `schedule`/`data_source_id` above).
    if "schedule_interval_seconds" in payload.model_fields_set:
        if payload.schedule_interval_seconds is None:
            task.schedule_interval_seconds = None
            task.next_run_at = None
        else:
            task.schedule_interval_seconds = payload.schedule_interval_seconds
            task.next_run_at = _compute_next_run_at(payload.schedule_interval_seconds)

    # Re-validated against the task's FINAL state (not just this request's
    # own fields): a request that changes task_type away from SYNC while
    # leaving an already-set schedule_interval_seconds untouched must be
    # rejected too, or the SYNC-only invariant could be silently violated
    # without ever touching the schedule field in that same request.
    _validate_schedule_interval_task_type(task.task_type, task.schedule_interval_seconds)

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
    prior SYNC run's DataProfile to clean), for STANDARDIZE tasks (which
    prior TRANSFORM run's approved CleaningRun to standardize), for MATCH
    tasks (which prior STANDARDIZE run's approved StandardizationRun to
    match/deduplicate), and, as of Module 9, for EXPORT tasks too (which
    prior MATCH run's approved MatchRun to materialize) -- same
    required/rejected branch extended to a fourth task_type, still
    rejected for every other task type, so the field's meaning can never
    be ambiguous per task. This API-layer check only confirms the
    referenced run exists in the same org; the deeper "must be an
    approved MatchRun" check for EXPORT stays in ExportHandler, exactly
    as MATCH's StandardizationRun check stays in MatchHandler."""
    # Inactive or cross-org task -> 404, same as any other direct access.
    task = _get_active_task_or_404(db, task_id, current_user.organization_id)

    source_task_run_id = payload.source_task_run_id if payload is not None else None

    if task.task_type in (
        TaskType.TRANSFORM, TaskType.STANDARDIZE, TaskType.MATCH, TaskType.EXPORT
    ):
        if source_task_run_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "source_task_run_id is required for TRANSFORM, STANDARDIZE, "
                    "MATCH, and EXPORT tasks"
                ),
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
            detail=(
                "source_task_run_id is only valid for TRANSFORM, STANDARDIZE, "
                "MATCH, and EXPORT tasks"
            ),
        )

    run = create_task_run_record(
        db,
        organization_id=current_user.organization_id,
        task_id=task.id,
        triggered_by=current_user.id,
        source_task_run_id=source_task_run_id,
    )
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


# --- Standardization results (Module 7 sub-resource) -------------------------


def _get_standardization_run_or_404(
    db: Session, task_id: uuid.UUID, run_id: uuid.UUID, org_id: uuid.UUID
) -> StandardizationRun:
    """Shared 404 chain for every standardization-result endpoint: task
    visible -> run visible -> standardization result exists. Direct mirror
    of _get_cleaning_run_or_404."""
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

    standardization_run = db.execute(
        select(StandardizationRun).where(
            StandardizationRun.task_run_id == run_id,
            StandardizationRun.task_id == task.id,
            StandardizationRun.organization_id == org_id,
        )
    ).scalar_one_or_none()
    if standardization_run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Standardization result not found"
        )
    return standardization_run


@router.get("/{task_id}/runs/{run_id}/standardization", response_model=StandardizationRunRead)
def get_task_run_standardization(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> StandardizationRun:
    """Module 7: the summary result of a standardization TaskRun -- counts,
    confidence, output location/hash, and current approval status. 404 if
    the run isn't visible to this org, or no standardization result exists
    yet (e.g. the run hasn't completed, or wasn't a STANDARDIZE run)."""
    return _get_standardization_run_or_404(db, task_id, run_id, current_user.organization_id)


@router.get(
    "/{task_id}/runs/{run_id}/standardization/changes",
    response_model=PaginatedResponse[StandardizationChangeRead],
)
def list_task_run_standardization_changes(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    pagination: PaginationParams = Depends(),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> PaginatedResponse[StandardizationChangeRead]:
    """Module 7: the bounded per-cell change log for a standardization run,
    in row order -- same pagination shape as list_task_run_cleaning_changes.
    Note this may under-represent total_changes_count on StandardizationRun
    for a run whose change volume exceeded
    STANDARDIZATION_MAX_PERSISTED_CHANGES; the aggregate count on the parent
    StandardizationRun is always accurate even when the per-change rows are
    capped."""
    standardization_run = _get_standardization_run_or_404(
        db, task_id, run_id, current_user.organization_id
    )

    filters = [
        StandardizationChange.standardization_run_id == standardization_run.id,
        StandardizationChange.organization_id == current_user.organization_id,
    ]
    total = db.execute(
        select(func.count()).select_from(StandardizationChange).where(*filters)
    ).scalar_one()
    rows = db.execute(
        select(StandardizationChange)
        .where(*filters)
        .order_by(StandardizationChange.row_index)
        .limit(pagination.limit)
        .offset(pagination.offset)
    ).scalars().all()

    return PaginatedResponse(
        items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset
    )


@router.post(
    "/{task_id}/runs/{run_id}/standardization/approve", response_model=StandardizationRunRead
)
def approve_task_run_standardization(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> StandardizationRun:
    """Module 7 approval state machine: pending_review -> approved only.
    Direct mirror of approve_task_run_cleaning -- any other starting status
    is a 409 conflict, not a 400."""
    standardization_run = _get_standardization_run_or_404(
        db, task_id, run_id, current_user.organization_id
    )
    if standardization_run.status != "pending_review":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot approve a standardization run with status '{standardization_run.status}'",
        )
    standardization_run.status = "approved"
    standardization_run.approved_by = current_user.id
    standardization_run.approved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(standardization_run)
    return standardization_run


@router.post(
    "/{task_id}/runs/{run_id}/standardization/reject", response_model=StandardizationRunRead
)
def reject_task_run_standardization(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> StandardizationRun:
    """Module 7 approval state machine: pending_review -> rejected only."""
    standardization_run = _get_standardization_run_or_404(
        db, task_id, run_id, current_user.organization_id
    )
    if standardization_run.status != "pending_review":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot reject a standardization run with status '{standardization_run.status}'",
        )
    standardization_run.status = "rejected"
    standardization_run.rejected_by = current_user.id
    standardization_run.rejected_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(standardization_run)
    return standardization_run


@router.post(
    "/{task_id}/runs/{run_id}/standardization/rollback", response_model=StandardizationRunRead
)
def rollback_task_run_standardization(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> StandardizationRun:
    """Module 7 approval state machine: approved -> rolled_back only (a
    rejected or already-rolled-back run cannot be rolled back). A pure
    status transition -- the output file and every StandardizationChange
    row are untouched, per the design doc's non-destructive rollback
    guarantee."""
    standardization_run = _get_standardization_run_or_404(
        db, task_id, run_id, current_user.organization_id
    )
    if standardization_run.status != "approved":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot roll back a standardization run with status '{standardization_run.status}'",
        )
    standardization_run.status = "rolled_back"
    standardization_run.rolled_back_by = current_user.id
    standardization_run.rolled_back_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(standardization_run)
    return standardization_run


# --- Standardization configuration (Module 7 org-level CRUD) -----------------
#
# Unlike everything else in this router, these two resources are not
# task-run-scoped -- they are organization-wide configuration consulted by
# StandardizationHandler on every run (see app/worker/handlers/
# standardization.py's _load_column_overrides/_load_lookup_tables). Kept in
# this same file/router per the design doc Section 5 ("all under the
# existing tasks router"), under a new but consistent path prefix
# (/tasks/standardization/...) rather than nested under a specific task or
# run, since the configuration itself applies across every STANDARDIZE task
# in the organization. Soft-delete via is_active=False, exactly like
# DELETE /tasks/{id} and DELETE /data-sources/{id} -- never a hard delete,
# so historical StandardizationChange rows that cite a rule stay
# interpretable even after the configuration that produced them changes.


@router.post(
    "/standardization/column-mappings",
    response_model=StandardizationColumnMappingRead,
    status_code=status.HTTP_201_CREATED,
)
def create_standardization_column_mapping(
    payload: StandardizationColumnMappingCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> StandardizationColumnMapping:
    """Declare (or override) which field_type a column should be classified
    as, either for one data source or (data_source_id omitted) org-wide.
    A cross-org or inactive data_source_id 404s, same as every other
    reference to a DataSource in this API."""
    if payload.data_source_id is not None:
        _validate_data_source_ref(db, payload.data_source_id, current_user.organization_id)

    mapping = StandardizationColumnMapping(
        organization_id=current_user.organization_id,
        data_source_id=payload.data_source_id,
        column_name=payload.column_name,
        field_type=payload.field_type,
        created_by=current_user.id,
    )
    db.add(mapping)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"An active mapping for column '{payload.column_name}' already exists "
                "for this scope"
            ),
        )
    db.refresh(mapping)
    return mapping


@router.get(
    "/standardization/column-mappings",
    response_model=PaginatedResponse[StandardizationColumnMappingRead],
)
def list_standardization_column_mappings(
    pagination: PaginationParams = Depends(),
    include_inactive: bool = Query(default=False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> PaginatedResponse[StandardizationColumnMappingRead]:
    filters = [StandardizationColumnMapping.organization_id == current_user.organization_id]
    if not include_inactive:
        filters.append(StandardizationColumnMapping.is_active.is_(True))

    total = db.execute(
        select(func.count()).select_from(StandardizationColumnMapping).where(*filters)
    ).scalar_one()
    rows = db.execute(
        select(StandardizationColumnMapping)
        .where(*filters)
        .order_by(StandardizationColumnMapping.created_at.desc())
        .limit(pagination.limit)
        .offset(pagination.offset)
    ).scalars().all()

    return PaginatedResponse(
        items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset
    )


@router.delete("/standardization/column-mappings/{mapping_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_standardization_column_mapping(
    mapping_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> None:
    mapping = db.execute(
        select(StandardizationColumnMapping).where(
            StandardizationColumnMapping.id == mapping_id,
            StandardizationColumnMapping.organization_id == current_user.organization_id,
            StandardizationColumnMapping.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if mapping is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Column mapping not found")
    mapping.is_active = False
    db.commit()


@router.post(
    "/standardization/lookup-entries",
    response_model=StandardizationLookupEntryRead,
    status_code=status.HTTP_201_CREATED,
)
def create_standardization_lookup_entry(
    payload: StandardizationLookupEntryCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> StandardizationLookupEntry:
    """Add an organization-supplied lookup-table entry (abbreviation
    expansion, canonical company suffix, country-name variant, etc.),
    either scoped to one field_type or (field_type omitted) applied across
    every classified field. Takes precedence over the engine's built-in
    default for the same key -- see app/standardization/rules/."""
    entry = StandardizationLookupEntry(
        organization_id=current_user.organization_id,
        field_type=payload.field_type,
        lookup_key=payload.lookup_key,
        lookup_value=payload.lookup_value,
        created_by=current_user.id,
    )
    db.add(entry)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"An active lookup entry for key '{payload.lookup_key}' already exists for this scope",
        )
    db.refresh(entry)
    return entry


@router.get(
    "/standardization/lookup-entries",
    response_model=PaginatedResponse[StandardizationLookupEntryRead],
)
def list_standardization_lookup_entries(
    pagination: PaginationParams = Depends(),
    include_inactive: bool = Query(default=False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> PaginatedResponse[StandardizationLookupEntryRead]:
    filters = [StandardizationLookupEntry.organization_id == current_user.organization_id]
    if not include_inactive:
        filters.append(StandardizationLookupEntry.is_active.is_(True))

    total = db.execute(
        select(func.count()).select_from(StandardizationLookupEntry).where(*filters)
    ).scalar_one()
    rows = db.execute(
        select(StandardizationLookupEntry)
        .where(*filters)
        .order_by(StandardizationLookupEntry.created_at.desc())
        .limit(pagination.limit)
        .offset(pagination.offset)
    ).scalars().all()

    return PaginatedResponse(
        items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset
    )


@router.delete("/standardization/lookup-entries/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_standardization_lookup_entry(
    entry_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> None:
    entry = db.execute(
        select(StandardizationLookupEntry).where(
            StandardizationLookupEntry.id == entry_id,
            StandardizationLookupEntry.organization_id == current_user.organization_id,
            StandardizationLookupEntry.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if entry is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lookup entry not found")
    entry.is_active = False
    db.commit()


# --- Module 8: data matching & deduplication --------------------------------


def _get_match_run_or_404(
    db: Session, task_id: uuid.UUID, run_id: uuid.UUID, org_id: uuid.UUID
) -> MatchRun:
    """Shared 404 chain for every matching-result endpoint: task visible ->
    run visible -> match result exists. Direct mirror of
    _get_standardization_run_or_404."""
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

    match_run = db.execute(
        select(MatchRun).where(
            MatchRun.task_run_id == run_id,
            MatchRun.task_id == task.id,
            MatchRun.organization_id == org_id,
        )
    ).scalar_one_or_none()
    if match_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Match result not found")
    return match_run


@router.get("/{task_id}/runs/{run_id}/matching", response_model=MatchRunRead)
def get_task_run_matching(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> MatchRun:
    """Module 8: the summary result of a MATCH TaskRun -- counts,
    confidence, and current approval status. No output-file fields --
    Module 8 produces no output file (see
    docs/module-8-data-matching-deduplication-design.md Section 2). 404 if
    the run isn't visible to this org, or no match result exists yet."""
    return _get_match_run_or_404(db, task_id, run_id, current_user.organization_id)


@router.get(
    "/{task_id}/runs/{run_id}/matching/groups",
    response_model=PaginatedResponse[MatchGroupRead],
)
def list_task_run_matching_groups(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    pagination: PaginationParams = Depends(),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> PaginatedResponse[MatchGroupRead]:
    """Module 8: the duplicate clusters found by a MATCH run, in
    canonical_row_index order. Full group membership is reconstructable
    from GET .../matching/decisions?match_group_id=... (record_a_row_index/
    record_b_row_index across every decision in that group)."""
    match_run = _get_match_run_or_404(db, task_id, run_id, current_user.organization_id)

    filters = [
        MatchGroup.match_run_id == match_run.id,
        MatchGroup.organization_id == current_user.organization_id,
    ]
    total = db.execute(select(func.count()).select_from(MatchGroup).where(*filters)).scalar_one()
    rows = db.execute(
        select(MatchGroup)
        .where(*filters)
        .order_by(MatchGroup.canonical_row_index)
        .limit(pagination.limit)
        .offset(pagination.offset)
    ).scalars().all()

    return PaginatedResponse(
        items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset
    )


@router.get(
    "/{task_id}/runs/{run_id}/matching/decisions",
    response_model=PaginatedResponse[MatchDecisionRead],
)
def list_task_run_matching_decisions(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    pagination: PaginationParams = Depends(),
    decision: str | None = Query(default=None),
    match_group_id: uuid.UUID | None = Query(default=None),
    blocking_key: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> PaginatedResponse[MatchDecisionRead]:
    """Module 8: the bounded pairwise-comparison audit log for a match
    run. ?decision=duplicate/?decision=ambiguous is the organization's
    reviewable "ambiguous match" queue -- a dedicated surface for exactly
    the records the acceptance criteria say must never be silently
    merged. ?match_group_id=... shows every decision that contributed to
    one specific group. ?blocking_key=... (new in the approved design
    revision) is the direct query surface for "why was this pair
    compared" -- see GET .../matching/skipped-blocks for the mirror-image
    "why was this pair never compared" question. Note this may
    under-represent duplicate_pairs_count/ambiguous_pairs_count on
    MatchRun for a run whose comparison volume exceeded
    MATCH_MAX_PERSISTED_DECISIONS; the aggregate counts on the parent
    MatchRun are always accurate even when the per-decision rows are
    capped."""
    match_run = _get_match_run_or_404(db, task_id, run_id, current_user.organization_id)

    if decision is not None and decision not in ("duplicate", "ambiguous"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="decision must be 'duplicate' or 'ambiguous'",
        )

    filters = [
        MatchDecision.match_run_id == match_run.id,
        MatchDecision.organization_id == current_user.organization_id,
    ]
    if decision is not None:
        filters.append(MatchDecision.decision == decision)
    if match_group_id is not None:
        filters.append(MatchDecision.match_group_id == match_group_id)
    if blocking_key is not None:
        filters.append(MatchDecision.blocking_key == blocking_key)

    total = db.execute(
        select(func.count()).select_from(MatchDecision).where(*filters)
    ).scalar_one()
    rows = db.execute(
        select(MatchDecision)
        .where(*filters)
        .order_by(MatchDecision.record_a_row_index, MatchDecision.record_b_row_index)
        .limit(pagination.limit)
        .offset(pagination.offset)
    ).scalars().all()

    return PaginatedResponse(
        items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset
    )


@router.get(
    "/{task_id}/runs/{run_id}/matching/skipped-blocks",
    response_model=PaginatedResponse[MatchSkippedBlockRead],
)
def list_task_run_matching_skipped_blocks(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    pagination: PaginationParams = Depends(),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> PaginatedResponse[MatchSkippedBlockRead]:
    """Module 8 (new in the approved design revision): the bounded audit
    log of blocks skipped for exceeding MATCH_MAX_BLOCK_SIZE -- the direct
    query surface for "why was this pair never compared." Bounded by
    construction (row_count / MATCH_MAX_BLOCK_SIZE per run), so pagination
    here is a formality, not a necessity."""
    match_run = _get_match_run_or_404(db, task_id, run_id, current_user.organization_id)

    filters = [
        MatchSkippedBlock.match_run_id == match_run.id,
        MatchSkippedBlock.organization_id == current_user.organization_id,
    ]
    total = db.execute(
        select(func.count()).select_from(MatchSkippedBlock).where(*filters)
    ).scalar_one()
    rows = db.execute(
        select(MatchSkippedBlock)
        .where(*filters)
        .order_by(MatchSkippedBlock.blocking_key)
        .limit(pagination.limit)
        .offset(pagination.offset)
    ).scalars().all()

    return PaginatedResponse(
        items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset
    )


@router.post("/{task_id}/runs/{run_id}/matching/approve", response_model=MatchRunRead)
def approve_task_run_matching(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> MatchRun:
    """Module 8 approval state machine: pending_review -> approved only.
    Direct mirror of approve_task_run_standardization. A pure status
    transition -- it does not merge, delete, or modify any record, any
    file, or any other database row; Module 8 never performs any physical
    merge or deletion at any point in its lifecycle, approved or not (see
    design doc Section 2/10)."""
    match_run = _get_match_run_or_404(db, task_id, run_id, current_user.organization_id)
    if match_run.status != "pending_review":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot approve a match run with status '{match_run.status}'",
        )
    match_run.status = "approved"
    match_run.approved_by = current_user.id
    match_run.approved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(match_run)
    return match_run


@router.post("/{task_id}/runs/{run_id}/matching/reject", response_model=MatchRunRead)
def reject_task_run_matching(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> MatchRun:
    """Module 8 approval state machine: pending_review -> rejected only."""
    match_run = _get_match_run_or_404(db, task_id, run_id, current_user.organization_id)
    if match_run.status != "pending_review":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot reject a match run with status '{match_run.status}'",
        )
    match_run.status = "rejected"
    match_run.rejected_by = current_user.id
    match_run.rejected_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(match_run)
    return match_run


@router.post("/{task_id}/runs/{run_id}/matching/rollback", response_model=MatchRunRead)
def rollback_task_run_matching(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> MatchRun:
    """Module 8 approval state machine: approved -> rolled_back only. A
    pure status transition -- every MatchGroup/MatchDecision/
    MatchSkippedBlock row is untouched, and since nothing destructive ever
    happened at approval time in the first place (Section 2/10), this is a
    structurally simpler, lower-risk rollback than Modules 6/7's."""
    match_run = _get_match_run_or_404(db, task_id, run_id, current_user.organization_id)
    if match_run.status != "approved":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot roll back a match run with status '{match_run.status}'",
        )
    match_run.status = "rolled_back"
    match_run.rolled_back_by = current_user.id
    match_run.rolled_back_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(match_run)
    return match_run


# --- Match rule-set configuration (Module 8 org-level CRUD) ------------------
#
# Not task-run-scoped -- organization-wide configuration MatchHandler
# consults on every run (see app/worker/handlers/matching.py's
# _load_rule_set). Same "/tasks/matching/..." path-prefix precedent
# Module 7's standardization config endpoints already established. Rule
# sets (and their field lists) are immutable once created: no PATCH/PUT,
# and no DELETE -- creating a new version automatically deactivates the
# prior active one for the same scope in the same transaction (soft
# "supersede," never a hard delete), so every historical MatchRun.
# rule_set_id/rule_set_version stays resolvable.


@router.post(
    "/matching/rule-sets", response_model=MatchRuleSetRead, status_code=status.HTTP_201_CREATED
)
def create_match_rule_set(
    payload: MatchRuleSetCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> MatchRuleSet:
    """Create a new, versioned MatchRuleSet (and its full field list) for
    an organization, either scoped to one data source or (data_source_id
    omitted) applied org-wide. version is computed server-side; creating
    a new rule set for a scope deactivates any prior active rule set for
    that same scope in the same transaction -- the old version remains
    readable (is_active=false), never deleted, so any MatchRun that cites
    it stays fully interpretable."""
    if payload.data_source_id is not None:
        _validate_data_source_ref(db, payload.data_source_id, current_user.organization_id)

    existing_count = db.execute(
        select(func.count())
        .select_from(MatchRuleSet)
        .where(
            MatchRuleSet.organization_id == current_user.organization_id,
            MatchRuleSet.data_source_id == payload.data_source_id,
        )
    ).scalar_one()

    prior_active = db.execute(
        select(MatchRuleSet).where(
            MatchRuleSet.organization_id == current_user.organization_id,
            MatchRuleSet.data_source_id == payload.data_source_id,
            MatchRuleSet.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if prior_active is not None:
        prior_active.is_active = False

    rule_set = MatchRuleSet(
        organization_id=current_user.organization_id,
        data_source_id=payload.data_source_id,
        version=existing_count + 1,
        duplicate_threshold=payload.duplicate_threshold,
        review_threshold=payload.review_threshold,
        created_by=current_user.id,
    )
    db.add(rule_set)
    db.flush()
    for field_payload in payload.fields:
        db.add(
            MatchRuleField(
                organization_id=current_user.organization_id,
                rule_set_id=rule_set.id,
                column_name=field_payload.column_name,
                comparison_type=field_payload.comparison_type,
                weight=field_payload.weight,
            )
        )
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An active rule set for this scope was just created by another request",
        )
    db.refresh(rule_set)
    return rule_set


@router.get("/matching/rule-sets", response_model=PaginatedResponse[MatchRuleSetRead])
def list_match_rule_sets(
    pagination: PaginationParams = Depends(),
    include_inactive: bool = Query(default=False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> PaginatedResponse[MatchRuleSetRead]:
    filters = [MatchRuleSet.organization_id == current_user.organization_id]
    if not include_inactive:
        filters.append(MatchRuleSet.is_active.is_(True))

    total = db.execute(select(func.count()).select_from(MatchRuleSet).where(*filters)).scalar_one()
    rows = db.execute(
        select(MatchRuleSet)
        .where(*filters)
        .order_by(MatchRuleSet.created_at.desc())
        .limit(pagination.limit)
        .offset(pagination.offset)
    ).scalars().all()

    return PaginatedResponse(
        items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset
    )


# --- Module 9: data export engine ------------------------------------------
#
# Consumes an APPROVED MatchRun (Module 8) and materializes it into a
# real deduplicated output CSV. Endpoint shape is a direct structural
# mirror of the matching-result endpoints above (_get_match_run_or_404 /
# get_task_run_matching / approve/reject/rollback), extended with a
# summary that DOES include output_sha256/file metadata (unlike
# MatchRunRead) since Export -- unlike Match -- writes a real file. No
# configuration-CRUD endpoints exist for Module 9: there is no
# organization-configurable export behavior in this release (see design
# doc Section 5's non-goals). Module 10 removed output_file_path from
# this summary (see docs/module-10-artifact-retrieval-design.md Section
# 13) -- retrieve the artifact itself via
# GET .../export/download.


def _get_export_run_or_404(
    db: Session, task_id: uuid.UUID, run_id: uuid.UUID, org_id: uuid.UUID
) -> ExportRun:
    """Shared 404 chain for every export-result endpoint: task visible ->
    run visible -> export result exists. Direct mirror of
    _get_match_run_or_404."""
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

    export_run = db.execute(
        select(ExportRun).where(
            ExportRun.task_run_id == run_id,
            ExportRun.task_id == task.id,
            ExportRun.organization_id == org_id,
        )
    ).scalar_one_or_none()
    if export_run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Export result not found"
        )
    return export_run


@router.get("/{task_id}/runs/{run_id}/export", response_model=ExportRunRead)
def get_task_run_export(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> ExportRun:
    """Module 9: the summary result of an EXPORT TaskRun -- row counts,
    the output file's location/hash/size/column-count/schema-version, and
    current approval status. The exported CSV's column layout is
    [...original standardized columns in their existing order...,
    __aiops_canonical_record (boolean), __aiops_source_row_index
    (integer)] -- both reserved, non-configurable, and guaranteed absent
    from the *input* header for any run that reached this endpoint (a
    collision there fails the run permanently before an ExportRun is ever
    created -- see ExportHandler). export_timestamp is database metadata
    only and is never present inside the CSV file itself. 404 if the run
    isn't visible to this org, or no export result exists yet."""
    return _get_export_run_or_404(db, task_id, run_id, current_user.organization_id)


@router.get(
    "/{task_id}/runs/{run_id}/export/exclusions",
    response_model=PaginatedResponse[ExportRowExclusionRead],
)
def list_task_run_export_exclusions(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    pagination: PaginationParams = Depends(),
    match_group_id: uuid.UUID | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> PaginatedResponse[ExportRowExclusionRead]:
    """Module 9: the bounded audit log of rows excluded from an export --
    the direct query surface for "why is this row missing from my
    exported file." ?match_group_id=... shows every row excluded because
    of one specific Module 8 duplicate group; cross-reference GET
    .../matching/decisions?match_group_id=... for the deeper "why was
    this row grouped" question, already answered by Module 8's own audit
    trail. Note this may under-represent excluded_row_count on ExportRun
    for a run whose exclusion volume exceeded
    EXPORT_MAX_PERSISTED_EXCLUSIONS; the aggregate count on the parent
    ExportRun is always accurate even when the per-row detail rows are
    capped."""
    export_run = _get_export_run_or_404(db, task_id, run_id, current_user.organization_id)

    filters = [
        ExportRowExclusion.export_run_id == export_run.id,
        ExportRowExclusion.organization_id == current_user.organization_id,
    ]
    if match_group_id is not None:
        filters.append(ExportRowExclusion.match_group_id == match_group_id)

    total = db.execute(
        select(func.count()).select_from(ExportRowExclusion).where(*filters)
    ).scalar_one()
    rows = db.execute(
        select(ExportRowExclusion)
        .where(*filters)
        .order_by(ExportRowExclusion.row_index)
        .limit(pagination.limit)
        .offset(pagination.offset)
    ).scalars().all()

    return PaginatedResponse(
        items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset
    )


@router.post("/{task_id}/runs/{run_id}/export/approve", response_model=ExportRunRead)
def approve_task_run_export(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> ExportRun:
    """Module 9 approval state machine: pending_review -> approved only.
    Direct mirror of approve_task_run_matching. A pure status transition
    -- it does not rewrite, move, or delete the output file, and does not
    trigger any further automatic action (no delivery, no cleanup)."""
    export_run = _get_export_run_or_404(db, task_id, run_id, current_user.organization_id)
    if export_run.status != "pending_review":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot approve an export run with status '{export_run.status}'",
        )
    export_run.status = "approved"
    export_run.approved_by = current_user.id
    export_run.approved_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(export_run)
    return export_run


@router.post("/{task_id}/runs/{run_id}/export/reject", response_model=ExportRunRead)
def reject_task_run_export(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> ExportRun:
    """Module 9 approval state machine: pending_review -> rejected only."""
    export_run = _get_export_run_or_404(db, task_id, run_id, current_user.organization_id)
    if export_run.status != "pending_review":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot reject an export run with status '{export_run.status}'",
        )
    export_run.status = "rejected"
    export_run.rejected_by = current_user.id
    export_run.rejected_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(export_run)
    return export_run


@router.post("/{task_id}/runs/{run_id}/export/rollback", response_model=ExportRunRead)
def rollback_task_run_export(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> ExportRun:
    """Module 9 approval state machine: approved -> rolled_back only. A
    pure status transition -- every ExportRowExclusion row and the output
    file itself are untouched; rollback never deletes the physical export
    file (same retention-policy gap already carried from Modules 6/7, now
    extended to a third output-producing module -- see design doc Section
    11/17)."""
    export_run = _get_export_run_or_404(db, task_id, run_id, current_user.organization_id)
    if export_run.status != "approved":
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot roll back an export run with status '{export_run.status}'",
        )
    export_run.status = "rolled_back"
    export_run.rolled_back_by = current_user.id
    export_run.rolled_back_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(export_run)
    return export_run


# --- Module 10: artifact retrieval (secure download) -------------------
#
# Serves the verified bytes of an APPROVED or ROLLED_BACK cleaning/
# standardization/export run's output file. No new TaskType, no worker
# changes -- purely additive API + one new audit table
# (ArtifactDownloadEvent) over files these three modules already write.
# See docs/module-10-artifact-retrieval-design.md.
#
# NO ARTIFACT BYTES ARE SENT BEFORE INTEGRITY VERIFICATION SUCCEEDS: the
# full artifact is re-hashed via bounded chunked reads, through a SINGLE
# open() call, before the HTTP response body begins; only a confirmed
# SHA-256 match causes any byte to be transmitted (Section 6/13).
#
# This is NOT a side-effect-free operation: it is artifact-read-only
# (the file itself is never modified) and audit-writing (exactly one
# ArtifactDownloadEvent row is created and later finalized per
# authorized attempt). Artifact content retrieval is deterministic and
# repeatable across requests; the audit side effect is intentionally
# non-idempotent per request -- each authorized attempt gets its own
# event, by design (Section 9's consistency correction).

# Maps each artifact type to the tenant-scoped root its output files are
# written under (app.core.config.Settings), and to the single non-null
# run-id column on ArtifactDownloadEvent it corresponds to.
_ARTIFACT_ROOT_SETTINGS = {
    "cleaning": "csv_output_root",
    "standardization": "csv_standardized_root",
    "export": "csv_exported_root",
}
_ARTIFACT_RUN_ID_FIELDS = {
    "cleaning": "cleaning_run_id",
    "standardization": "standardization_run_id",
    "export": "export_run_id",
}


def _finalize_download_event(
    event_id: uuid.UUID,
    *,
    outcome: str,
    failure_reason_code: str | None = None,
    verified_sha256: str | None = None,
    bytes_served: int | None = None,
) -> None:
    """Performs the ONE terminal update on an ArtifactDownloadEvent row --
    never a second insert for the same authorized attempt, and never
    updated again after this call. Uses its OWN, independent database
    session (SessionLocal directly, not the endpoint's injected
    Depends(get_db) session) because the success/failure path that
    matters most -- finalizing after a streamed transfer -- runs from
    inside a StreamingResponse generator, which the ASGI server drives
    AFTER the endpoint function has already returned; by that point the
    request's own injected session may already be closed. Used
    identically for the pre-stream failure paths (file_missing,
    integrity_failed) so there is exactly one finalization code path,
    not two."""
    session = SessionLocal()
    try:
        event = session.get(ArtifactDownloadEvent, event_id)
        if event is None:
            return
        event.outcome = outcome
        event.failure_reason_code = failure_reason_code
        if verified_sha256 is not None:
            event.verified_sha256 = verified_sha256
        if bytes_served is not None:
            event.bytes_served = bytes_served
        event.completed_at = datetime.now(timezone.utc)
        session.commit()
    finally:
        session.close()


def _download_artifact(
    db: Session,
    current_user: User,
    artifact_type: str,
    run: CleaningRun | StandardizationRun | ExportRun,
) -> StreamingResponse:
    """Shared verify-then-stream download logic for all three artifact
    types. Exact operation ordering (Section 9's consistency
    correction): resolve tenant-scoped run (by the caller, via the
    existing _get_*_run_or_404 helpers) -> validate downloadable state
    -> resolve and contain path -> create started audit row -> open and
    verify the full artifact (this single open() call also resolves
    file-existence/regular-file, folded into the same attempt rather
    than a separate stat -- see app.artifacts.download) -> begin
    streaming only after a confirmed hash match -> finalize the audit
    outcome once the transfer completes or fails.

    Downloadable-state policy (Section 11): approved is the current
    authoritative output; rolled_back is downloadable strictly for
    audit/investigation, explicitly NOT current authoritative output --
    the X-Artifact-Run-Status response header always carries the run's
    actual status so a client is never left to infer this. pending_review
    and rejected are blocked (409), and never create an audit row, since
    authorization never succeeded for them.
    """
    if run.status not in ("approved", "rolled_back"):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Cannot download an artifact with status '{run.status}'",
        )

    tenant_root = Path(
        getattr(get_settings(), _ARTIFACT_ROOT_SETTINGS[artifact_type])
    ) / str(current_user.organization_id)

    try:
        resolved_path = resolve_artifact_path(tenant_root, run.output_file_path)
    except ArtifactPathError:
        # Defense-in-depth only -- output_file_path is always written
        # server-side by CleaningHandler/StandardizationHandler/
        # ExportHandler, never client-supplied. Path containment is
        # validated before any audit row is created (canonical
        # ordering), so a violation here creates no row.
        logger.error(
            "artifact download path containment violation: artifact_type=%s run_id=%s",
            artifact_type, run.id,
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")

    event = ArtifactDownloadEvent(
        id=uuid.uuid4(),
        organization_id=current_user.organization_id,
        artifact_type=artifact_type,
        downloaded_by=current_user.id,
        run_status_at_request=run.status,
        outcome="started",
        **{_ARTIFACT_RUN_ID_FIELDS[artifact_type]: run.id},
    )
    db.add(event)
    db.commit()
    db.refresh(event)
    event_id = event.id

    try:
        fileobj = open_verified_artifact(resolved_path, run.output_sha256)
    except ArtifactMissingError as exc:
        _finalize_download_event(
            event_id, outcome="file_missing", failure_reason_code=exc.failure_reason_code
        )
        logger.error(
            "artifact download file missing or unreadable: artifact_type=%s run_id=%s "
            "reason=%s",
            artifact_type, run.id, exc.failure_reason_code,
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Artifact not found")
    except ArtifactIntegrityError:
        _finalize_download_event(
            event_id, outcome="integrity_failed", failure_reason_code="hash_mismatch"
        )
        # High-severity log, deliberately WITHOUT the expected/actual
        # hash values or the filesystem path -- those never appear in
        # any client-facing response either (Section 13).
        logger.critical(
            "ARTIFACT INTEGRITY VERIFICATION FAILED -- no bytes were sent: "
            "artifact_type=%s run_id=%s",
            artifact_type, run.id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Artifact integrity verification failed; download refused",
        )

    try:
        verified_size = os.fstat(fileobj.fileno()).st_size
    except OSError:
        # Extremely narrow window: verification just succeeded and the
        # descriptor is open and rewound, but the fstat() call itself
        # failed before streaming could begin. Without this branch the
        # descriptor would leak and the audit row would stay stuck at
        # 'started' forever -- every other failure branch in this
        # function already closes the file and reaches a terminal
        # outcome, so this one must too.
        fileobj.close()
        _finalize_download_event(
            event_id,
            outcome="stream_failed",
            failure_reason_code="io_error",
            verified_sha256=run.output_sha256,
            bytes_served=0,
        )
        logger.error(
            "artifact download failed after verification, before streaming began: "
            "artifact_type=%s run_id=%s",
            artifact_type, run.id,
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Artifact could not be prepared for download",
        )
    verified_sha256 = run.output_sha256

    def _stream() -> Iterator[bytes]:
        bytes_sent = 0
        completed = False
        try:
            for chunk in iter_artifact_chunks(fileobj):
                bytes_sent += len(chunk)
                yield chunk
            completed = True
        finally:
            _finalize_download_event(
                event_id,
                outcome="completed" if completed else "stream_failed",
                failure_reason_code=None if completed else "stream_interrupted",
                verified_sha256=verified_sha256,
                bytes_served=bytes_sent,
            )

    filename = safe_download_filename(artifact_type, run.id)
    headers = {
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length": str(verified_size),
        "X-Artifact-Run-Status": run.status,
    }
    return StreamingResponse(_stream(), media_type="text/csv", headers=headers)


@router.get("/{task_id}/runs/{run_id}/cleaning/download")
def download_task_run_cleaning(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> StreamingResponse:
    """Module 10: streams the verified bytes of a cleaning run's output
    CSV. 404 if the run isn't visible to this org or the artifact is
    missing/unreadable; 409 if the run is pending_review or rejected;
    500 if pre-stream integrity verification fails (no bytes sent in
    that case). See _download_artifact for the full flow."""
    cleaning_run = _get_cleaning_run_or_404(db, task_id, run_id, current_user.organization_id)
    return _download_artifact(db, current_user, "cleaning", cleaning_run)


@router.get("/{task_id}/runs/{run_id}/standardization/download")
def download_task_run_standardization(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> StreamingResponse:
    """Module 10: streams the verified bytes of a standardization run's
    output CSV. Same behavior/status codes as download_task_run_cleaning."""
    standardization_run = _get_standardization_run_or_404(
        db, task_id, run_id, current_user.organization_id
    )
    return _download_artifact(db, current_user, "standardization", standardization_run)


@router.get("/{task_id}/runs/{run_id}/export/download")
def download_task_run_export(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> StreamingResponse:
    """Module 10: streams the verified bytes of an export run's output
    CSV. Same behavior/status codes as download_task_run_cleaning."""
    export_run = _get_export_run_or_404(db, task_id, run_id, current_user.organization_id)
    return _download_artifact(db, current_user, "export", export_run)
