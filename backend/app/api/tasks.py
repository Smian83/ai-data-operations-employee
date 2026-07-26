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
from app.models.enums import (
    ISSUE_SEVERITIES,
    ISSUE_TYPES,
    QUALITY_CATEGORIES,
    QUALITY_FINDING_OUTCOMES,
    QUALITY_FINDING_SEVERITIES,
    REMEDIATION_ACTIONS,
    VALIDATION_OUTCOMES,
    VALIDATION_RULE_NAMES,
    TaskType,
)
from app.models.export_row_exclusion import ExportRowExclusion
from app.models.export_run import ExportRun
from app.models.issue import Issue
from app.models.issue_detection_run import IssueDetectionRun
from app.models.match_decision import MatchDecision
from app.models.match_group import MatchGroup
from app.models.match_rule_field import MatchRuleField
from app.models.match_rule_set import MatchRuleSet
from app.models.match_run import MatchRun
from app.models.match_skipped_block import MatchSkippedBlock
from app.models.remediation_change import RemediationChange
from app.models.remediation_change_decision import RemediationChangeDecision
from app.models.remediation_run import RemediationRun
from app.models.quality_control_run import QualityControlRun
from app.models.quality_finding import QualityFinding
from app.models.validation_result import ValidationResult
from app.models.validation_run import ValidationRun
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
from app.schemas.issue import IssueRead
from app.schemas.issue_detection_run import IssueDetectionRunRead
from app.schemas.match_decision import MatchDecisionRead
from app.schemas.match_group import MatchGroupRead
from app.schemas.match_rule_set import MatchRuleSetCreate, MatchRuleSetRead
from app.schemas.match_run import MatchRunRead
from app.schemas.remediation import (
    BulkDecisionResponse,
    RemediationChangeDecisionRead,
    RemediationChangeDecisionRequest,
    RemediationChangeRead,
    RemediationRunRead,
)
from app.schemas.quality_control import QualityControlRunRead, QualityFindingRead
from app.schemas.validation import ValidationResultRead, ValidationRunRead
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
    match/deduplicate), as of Module 9, for EXPORT tasks too (which prior
    MATCH run's approved MatchRun to materialize), and, as of Module 15
    Phase 3, for REMEDIATE tasks too (which prior DETECT run's Issues to
    propose corrections for), and, as of Module 17 Phase 3, for VALIDATE
    tasks too (which prior REMEDIATE run's approved changes to validate) --
    same required/rejected branch extended to a sixth task_type, still
    rejected for every other task type, so the field's meaning can never be
    ambiguous per task. This API-layer check only confirms the referenced
    run exists in the same org; the deeper checks stay in each handler.
    Note: unlike TRANSFORM/STANDARDIZE/MATCH/EXPORT, REMEDIATE's upstream
    run (IssueDetectionRun) has no status/approval gate at all -- see
    RemediationRun's own docstring -- so no equivalent "must be approved"
    check exists anywhere for REMEDIATE. VALIDATE similarly has no
    API-level approval gate (the approval lives on RemediationChangeDecision
    rows, not on RemediationRun itself)."""
    # Inactive or cross-org task -> 404, same as any other direct access.
    task = _get_active_task_or_404(db, task_id, current_user.organization_id)

    source_task_run_id = payload.source_task_run_id if payload is not None else None

    if task.task_type in (
        TaskType.TRANSFORM, TaskType.STANDARDIZE, TaskType.MATCH, TaskType.EXPORT,
        TaskType.REMEDIATE, TaskType.VALIDATE,
    ):
        if source_task_run_id is None:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=(
                    "source_task_run_id is required for TRANSFORM, STANDARDIZE, "
                    "MATCH, EXPORT, REMEDIATE, and VALIDATE tasks"
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
                "MATCH, EXPORT, REMEDIATE, and VALIDATE tasks"
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
    correction, extended by Module 13): resolve tenant-scoped run (by
    the caller, via the existing _get_*_run_or_404 helpers) -> validate
    downloadable state -> reject an already-purged artifact (Module 13:
    output_deleted_at IS NOT NULL, a single-shot "purged" audit row,
    410) -> resolve and contain path -> create started audit row ->
    open and verify the full artifact (this single open() call also
    resolves file-existence/regular-file, folded into the same attempt
    rather than a separate stat -- see app.artifacts.download) -> begin
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

    if run.output_deleted_at is not None:
        # Module 13: the artifact was already removed by the retention
        # module. This is checked before any path resolution or storage
        # access -- resolve_artifact_path() and ArtifactStorage.open() are
        # never reached for a purged artifact, since there is nothing on
        # disk to resolve or open. Written directly as a single-shot
        # terminal row (outcome="purged", no failure_reason_code, never a
        # 'started'-then-finalized transition) -- see
        # ArtifactDownloadEvent's own docstring for why this differs from
        # the file_missing/integrity_failed/stream_failed lifecycle below.
        # The response carries no filesystem path, no output_deleted_at
        # value, and no internal error detail -- only a short, generic
        # message.
        purged_event = ArtifactDownloadEvent(
            id=uuid.uuid4(),
            organization_id=current_user.organization_id,
            artifact_type=artifact_type,
            downloaded_by=current_user.id,
            run_status_at_request=run.status,
            outcome="purged",
            failure_reason_code=None,
            completed_at=datetime.now(timezone.utc),
            **{_ARTIFACT_RUN_ID_FIELDS[artifact_type]: run.id},
        )
        db.add(purged_event)
        db.commit()
        raise HTTPException(
            status_code=status.HTTP_410_GONE, detail="Artifact no longer available"
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


# --- Module 14 Phase 3: Issue Detection API ---------------------------------
#
# Read-only orchestration only -- this router never imports or calls
# anything from app.detection (the pure engine) or
# app.worker.handlers.issue_detection. Both endpoints below do nothing
# more than look up rows the worker already wrote (see
# app.worker.handlers.issue_detection.IssueDetectionHandler) and shape
# them into response DTOs; all detection business logic already ran
# inside the worker, synchronously, before either endpoint is ever
# called (Phase 3 correction #6 -- no async behavior is introduced here,
# and none is needed).
#
# Neither endpoint returns a SQLAlchemy model (Phase 3 correction #2):
# the summary endpoint returns IssueDetectionRunRead, itself built via
# Pydantic's from_attributes off the ORM row (schema-level DTO
# conversion, not the raw ORM object); the detail endpoint builds each
# IssueRead explicitly, field by field, including the joined-in
# dataset_id (see app.schemas.issue's own docstring).


def _get_issue_detection_run_or_404(
    db: Session, task_id: uuid.UUID, run_id: uuid.UUID, org_id: uuid.UUID
) -> IssueDetectionRun:
    """Shared 404 chain for every detection-result endpoint: task visible
    -> run visible -> detection result exists. Direct mirror of
    _get_cleaning_run_or_404 / _get_match_run_or_404."""
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

    detection_run = db.execute(
        select(IssueDetectionRun).where(
            IssueDetectionRun.task_run_id == run_id,
            IssueDetectionRun.task_id == task.id,
            IssueDetectionRun.organization_id == org_id,
        )
    ).scalar_one_or_none()
    if detection_run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Issue detection result not found"
        )
    return detection_run


@router.get("/{task_id}/runs/{run_id}/detection", response_model=IssueDetectionRunRead)
def get_task_run_detection(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> IssueDetectionRun:
    """Module 14: the summary result of a DETECT TaskRun -- scan counts,
    total_issues_found/persisted_issue_count, and the issues_by_severity/
    issues_by_type breakdowns. Phase 3 correction #5: this is the entire
    handler -- one row already computed by the worker is fetched and
    returned; no Issue row is ever loaded to answer this request. 404 if
    the run isn't visible to this org or no detection result exists yet
    (e.g. the run hasn't completed, or wasn't a DETECT run)."""
    return _get_issue_detection_run_or_404(db, task_id, run_id, current_user.organization_id)


# Query-param name -> Issue column, the single place a future filter is
# added (Phase 3 correction #4: "future filters can be added without
# rewriting endpoints"). list_task_run_detection_issues below only ever
# loops over this mapping -- adding a new filterable field is one new
# entry here plus one new typed Query(...) parameter on the endpoint
# signature (FastAPI/OpenAPI need an explicit, documented parameter per
# filter), never a change to the WHERE-building logic itself.
_ISSUE_FILTER_COLUMNS = {
    "severity": Issue.severity,
    "issue_type": Issue.issue_type,
    "column_name": Issue.column_name,
    "row_number": Issue.row_number,
}


def _apply_issue_filters(filters: list, **filter_values) -> list:
    for name, value in filter_values.items():
        if value is not None:
            filters.append(_ISSUE_FILTER_COLUMNS[name] == value)
    return filters


@router.get(
    "/{task_id}/runs/{run_id}/detection/issues",
    response_model=PaginatedResponse[IssueRead],
)
def list_task_run_detection_issues(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    pagination: PaginationParams = Depends(),
    severity: str | None = Query(default=None),
    issue_type: str | None = Query(default=None),
    column_name: str | None = Query(default=None),
    row_number: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> PaginatedResponse[IssueRead]:
    """Module 14: the bounded per-finding list for a detection run, in
    row order -- same server-side limit/offset pagination shape as every
    other *_changes/*_decisions list endpoint (Phase 3 correction #3: full
    result sets are never returned in one response; PaginationParams caps
    `limit` at 100). Filterable by severity, issue_type, column_name, and
    row_number (Phase 3 correction #4), individually or combined -- see
    _ISSUE_FILTER_COLUMNS. Note this may under-represent
    total_issues_found on IssueDetectionRun for a run whose finding
    volume exceeded ISSUE_DETECTION_MAX_PERSISTED_ISSUES; the aggregate
    counts on the parent IssueDetectionRun (surfaced via GET
    .../detection) are always accurate even when the per-issue rows here
    are capped."""
    detection_run = _get_issue_detection_run_or_404(
        db, task_id, run_id, current_user.organization_id
    )

    if severity is not None and severity not in ISSUE_SEVERITIES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"severity must be one of {ISSUE_SEVERITIES}",
        )
    if issue_type is not None and issue_type not in ISSUE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"issue_type must be one of {ISSUE_TYPES}",
        )

    filters = [
        Issue.detection_run_id == detection_run.id,
        Issue.organization_id == current_user.organization_id,
    ]
    _apply_issue_filters(
        filters,
        severity=severity,
        issue_type=issue_type,
        column_name=column_name,
        row_number=row_number,
    )

    total = db.execute(select(func.count()).select_from(Issue).where(*filters)).scalar_one()
    rows = db.execute(
        select(Issue)
        .where(*filters)
        .order_by(Issue.row_number, Issue.id)
        .limit(pagination.limit)
        .offset(pagination.offset)
    ).scalars().all()

    items = [
        IssueRead(
            id=row.id,
            detection_run_id=row.detection_run_id,
            dataset_id=detection_run.data_source_id,
            row_number=row.row_number,
            column_name=row.column_name,
            issue_type=row.issue_type,
            severity=row.severity,
            original_value=row.original_value,
            suggested_fix=row.suggested_fix,
            confidence=row.confidence,
            created_at=row.created_at,
        )
        for row in rows
    ]

    return PaginatedResponse(
        items=items, total=total, limit=pagination.limit, offset=pagination.offset
    )


# --- Module 15 Phase 4: Remediation API -------------------------------------
#
# Read-only orchestration only, same discipline as the Module 14 Phase 3
# section above: this router never imports or calls anything from
# app.remediation (the pure engine) or app.worker.handlers.remediation.
# Both endpoints below only look up rows the worker already wrote (see
# RemediationHandler) and shape them into response DTOs -- all remediation
# business logic already ran inside the worker, synchronously, before
# either endpoint is ever called. No write operations, no approval/apply
# endpoints, no source data ever read or touched here.
#
# Neither endpoint returns a SQLAlchemy model: the summary endpoint
# returns RemediationRunRead, built explicitly field by field (including
# the two joined-in/derived fields dataset_sha256 and
# processing_duration_ms -- see app.schemas.remediation's own docstring
# for why those are computed here rather than duplicated as columns); the
# detail endpoint builds each RemediationChangeRead explicitly, exactly
# like IssueRead already does for Module 14.


def _get_remediation_run_or_404(
    db: Session, task_id: uuid.UUID, run_id: uuid.UUID, org_id: uuid.UUID
) -> tuple[RemediationRun, TaskRun]:
    """Shared 404 chain for every remediation-result endpoint: task
    visible -> run visible -> remediation result exists. Direct mirror of
    _get_issue_detection_run_or_404, except it also returns the TaskRun
    row itself (not just confirms its existence) -- the summary endpoint
    needs TaskRun.started_at/finished_at to compute processing_duration_ms
    without a second query."""
    task = _get_active_task_or_404(db, task_id, org_id)
    task_run = db.execute(
        select(TaskRun).where(
            TaskRun.id == run_id,
            TaskRun.task_id == task.id,
            TaskRun.organization_id == org_id,
        )
    ).scalar_one_or_none()
    if task_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task run not found")

    remediation_run = db.execute(
        select(RemediationRun).where(
            RemediationRun.task_run_id == run_id,
            RemediationRun.task_id == task.id,
            RemediationRun.organization_id == org_id,
        )
    ).scalar_one_or_none()
    if remediation_run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Remediation result not found"
        )
    return remediation_run, task_run


@router.get("/{task_id}/runs/{run_id}/remediation", response_model=RemediationRunRead)
def get_task_run_remediation(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> RemediationRunRead:
    """Module 15: the summary result of a REMEDIATE TaskRun -- aggregate
    counts, the changes_by_action/changes_by_column/skipped_by_reason
    breakdowns, engine version, the dataset hash the run was verified
    against, and processing duration. Cheap: every count either was
    already computed by RemediationHandler (changes_by_action,
    skipped_by_reason, the three top-level counts) or is a single
    GROUP BY / one joined row computed here (changes_by_column,
    dataset_sha256, processing_duration_ms) -- no RemediationChange row is
    ever loaded in full, and the pure engine is never re-run. 404 if the
    run isn't visible to this org or no remediation result exists yet."""
    remediation_run, task_run = _get_remediation_run_or_404(
        db, task_id, run_id, current_user.organization_id
    )

    # The upstream IssueDetectionRun this remediation was verified
    # against -- always present in practice (RESTRICT FK,
    # RemediationHandler never persists without it), looked up defensively
    # rather than assumed.
    detection_run = db.execute(
        select(IssueDetectionRun).where(
            IssueDetectionRun.task_run_id == remediation_run.source_task_run_id,
            IssueDetectionRun.organization_id == current_user.organization_id,
        )
    ).scalar_one_or_none()

    processing_duration_ms = None
    if task_run.started_at is not None and task_run.finished_at is not None:
        processing_duration_ms = round(
            (task_run.finished_at - task_run.started_at).total_seconds() * 1000
        )

    column_counts = db.execute(
        select(RemediationChange.column_name, func.count())
        .where(
            RemediationChange.remediation_run_id == remediation_run.id,
            RemediationChange.column_name.is_not(None),
        )
        .group_by(RemediationChange.column_name)
    ).all()

    # Module 16 Phase 3: decision breakdown (single GROUP BY query, no N+1).
    # The 409 enforcement on approve/reject guarantees ≤1 decision row per
    # change, so GROUP BY on the decisions table gives exact per-outcome
    # counts. pending = total_changes - approved - rejected.
    decision_count_rows = db.execute(
        select(RemediationChangeDecision.decision, func.count().label("cnt"))
        .where(
            RemediationChangeDecision.remediation_run_id == remediation_run.id,
            RemediationChangeDecision.organization_id == current_user.organization_id,
        )
        .group_by(RemediationChangeDecision.decision)
    ).all()
    decided: dict[str, int] = {d: c for d, c in decision_count_rows}
    approved_count = decided.get("approved", 0)
    rejected_count = decided.get("rejected", 0)
    decision_summary = {
        "pending": max(0, remediation_run.total_changes_count - approved_count - rejected_count),
        "approved": approved_count,
        "rejected": rejected_count,
    }

    return RemediationRunRead(
        id=remediation_run.id,
        organization_id=remediation_run.organization_id,
        task_run_id=remediation_run.task_run_id,
        task_id=remediation_run.task_id,
        data_source_id=remediation_run.data_source_id,
        source_task_run_id=remediation_run.source_task_run_id,
        issues_considered_count=remediation_run.issues_considered_count,
        total_changes_count=remediation_run.total_changes_count,
        issues_skipped_count=remediation_run.issues_skipped_count,
        changes_by_action=remediation_run.changes_by_action,
        changes_by_column={name: count for name, count in column_counts},
        skipped_by_reason=remediation_run.skipped_by_reason,
        remediation_engine_version=remediation_run.remediation_engine_version,
        dataset_sha256=detection_run.source_sha256 if detection_run is not None else None,
        processing_duration_ms=processing_duration_ms,
        decision_summary=decision_summary,
        created_at=remediation_run.created_at,
    )


# Query-param name -> RemediationChange column, same single-source-of-truth
# mapping convention _ISSUE_FILTER_COLUMNS established for Module 14.
# skip_reason is deliberately NOT filterable here -- a RemediationChange
# row is, by construction, always a proposed change (RuleOutcome.changed
# was True); no change row ever carries a skip reason, so a skip_reason
# filter on this endpoint could never match anything. The skip-reason
# breakdown lives on the summary endpoint instead
# (RemediationRunRead.skipped_by_reason), which is the only place a
# reason-per-skip count is persisted at all (see that field's own
# docstring on RemediationRun).
_REMEDIATION_CHANGE_FILTER_COLUMNS = {
    "action": RemediationChange.action,
    "column_name": RemediationChange.column_name,
    "row_number": RemediationChange.row_number,
}


def _apply_remediation_change_filters(filters: list, **filter_values) -> list:
    for name, value in filter_values.items():
        if value is not None:
            filters.append(_REMEDIATION_CHANGE_FILTER_COLUMNS[name] == value)
    return filters


@router.get(
    "/{task_id}/runs/{run_id}/remediation/changes",
    response_model=PaginatedResponse[RemediationChangeRead],
)
def list_task_run_remediation_changes(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    pagination: PaginationParams = Depends(),
    action: str | None = Query(default=None),
    column_name: str | None = Query(default=None),
    row_number: int | None = Query(default=None, ge=0),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> PaginatedResponse[RemediationChangeRead]:
    """Module 15: the bounded per-proposal list for a remediation run, in
    stable (row_number, id) order regardless of insertion order -- same
    server-side limit/offset pagination shape as list_task_run_detection_
    issues. Filterable by action, column_name, and row_number,
    individually or combined -- see _REMEDIATION_CHANGE_FILTER_COLUMNS.
    total_changes_count on the parent RemediationRun (surfaced via GET
    .../remediation) is always accurate even when a filter narrows what
    this endpoint returns, since that count reflects every persisted
    change, not the current filtered page."""
    remediation_run, _ = _get_remediation_run_or_404(
        db, task_id, run_id, current_user.organization_id
    )

    if action is not None and action not in REMEDIATION_ACTIONS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"action must be one of {REMEDIATION_ACTIONS}",
        )

    filters = [
        RemediationChange.remediation_run_id == remediation_run.id,
        RemediationChange.organization_id == current_user.organization_id,
    ]
    _apply_remediation_change_filters(
        filters, action=action, column_name=column_name, row_number=row_number
    )

    total = db.execute(
        select(func.count()).select_from(RemediationChange).where(*filters)
    ).scalar_one()
    rows = db.execute(
        select(RemediationChange)
        .where(*filters)
        .order_by(RemediationChange.row_number, RemediationChange.id)
        .limit(pagination.limit)
        .offset(pagination.offset)
    ).scalars().all()

    # Module 16 Phase 3: batch-load effective decisions for this page (no N+1).
    page_change_ids = [row.id for row in rows]
    decision_status_map = _get_decision_status_map(
        db, page_change_ids, current_user.organization_id
    )

    items = [
        RemediationChangeRead(
            id=row.id,
            remediation_run_id=row.remediation_run_id,
            source_issue_id=row.source_issue_id,
            row_number=row.row_number,
            column_name=row.column_name,
            action=row.action,
            original_value=row.original_value,
            proposed_value=row.proposed_value,
            reason=row.reason,
            confidence=row.confidence,
            created_at=row.created_at,
            decision_status=decision_status_map.get(row.id, "pending"),
        )
        for row in rows
    ]

    return PaginatedResponse(
        items=items, total=total, limit=pagination.limit, offset=pagination.offset
    )


# ---------------------------------------------------------------------------
# Module 16: Approval Queue -- helpers, bulk ops, per-change endpoints
# ---------------------------------------------------------------------------


def _get_decision_status_map(
    db: Session,
    change_ids: list[uuid.UUID],
    organization_id: uuid.UUID,
) -> dict[uuid.UUID, str]:
    """Batch-load the effective decision status for a list of change IDs.

    Returns a dict mapping change_id → 'approved' | 'rejected'. Change IDs
    absent from the dict have no decision yet (status = 'pending').

    Loads all relevant rows ordered by decision_timestamp DESC, then picks
    the first occurrence of each change_id in Python. This gives the latest
    effective decision without DISTINCT ON (which is PostgreSQL-only). The
    entire batch is resolved in a single SQL query regardless of page size."""
    if not change_ids:
        return {}
    rows = db.execute(
        select(
            RemediationChangeDecision.remediation_change_id,
            RemediationChangeDecision.decision,
        )
        .where(
            RemediationChangeDecision.remediation_change_id.in_(change_ids),
            RemediationChangeDecision.organization_id == organization_id,
        )
        .order_by(RemediationChangeDecision.decision_timestamp.desc())
    ).all()
    status_map: dict[uuid.UUID, str] = {}
    for change_id, decision in rows:
        if change_id not in status_map:  # first = latest (DESC)
            status_map[change_id] = decision
    return status_map


def _bulk_decide(
    db: Session,
    remediation_run,
    decision_value: str,
    current_user: "User",
    body: "RemediationChangeDecisionRequest | None",
) -> "BulkDecisionResponse":
    """Shared logic for approve-all and reject-all.

    Algorithm (no N+1):
    1. Load all change IDs for this run in stable (row_number, id) order.
    2. Load all change IDs that already have any decision (single IN query).
    3. Insert RemediationChangeDecision rows for the pending set only,
       using db.add_all() for a single round-trip.
    4. Return counts.

    Immutability: existing decisions are never touched. This function only
    inserts; it never UPDATE or DELETE anything in remediation_change_decisions.
    Idempotent: calling twice with no intervening changes results in 0 new
    rows on the second call (all changes already have decisions).
    """
    org_id = remediation_run.organization_id

    # Step 1: all change IDs for this run, stable order
    all_change_ids: list[uuid.UUID] = db.execute(
        select(RemediationChange.id)
        .where(
            RemediationChange.remediation_run_id == remediation_run.id,
            RemediationChange.organization_id == org_id,
        )
        .order_by(RemediationChange.row_number, RemediationChange.id)
    ).scalars().all()

    total = len(all_change_ids)
    if total == 0:
        return BulkDecisionResponse(
            total_changes=0,
            approved_count=0,
            rejected_count=0,
            skipped_existing_count=0,
        )

    # Step 2: which change IDs already have at least one decision?
    already_decided_ids: set[uuid.UUID] = set(
        db.execute(
            select(RemediationChangeDecision.remediation_change_id)
            .where(
                RemediationChangeDecision.remediation_change_id.in_(all_change_ids),
                RemediationChangeDecision.organization_id == org_id,
            )
            .distinct()
        ).scalars().all()
    )

    pending_ids = [c for c in all_change_ids if c not in already_decided_ids]
    skipped = len(already_decided_ids)
    new_count = len(pending_ids)

    if pending_ids:
        now = datetime.now(timezone.utc)
        reviewer_name = current_user.full_name or current_user.email
        comment = body.comment if body else None

        # Step 3: bulk insert (single db.add_all, one INSERT per row but
        # flushed together -- avoids per-row network round-trips)
        db.add_all([
            RemediationChangeDecision(
                organization_id=org_id,
                remediation_run_id=remediation_run.id,
                remediation_change_id=change_id,
                decision=decision_value,
                reviewer_id=current_user.id,
                reviewer_name=reviewer_name,
                reviewer_role="superuser",
                decision_timestamp=now,
                comment=comment,
            )
            for change_id in pending_ids
        ])
        db.commit()

    approved_count = new_count if decision_value == "approved" else 0
    rejected_count = new_count if decision_value == "rejected" else 0
    return BulkDecisionResponse(
        total_changes=total,
        approved_count=approved_count,
        rejected_count=rejected_count,
        skipped_existing_count=skipped,
    )


@router.post(
    "/{task_id}/runs/{run_id}/remediation/approve-all",
    response_model=BulkDecisionResponse,
    status_code=status.HTTP_200_OK,
    tags=["tasks"],
)
def approve_all_remediation_changes(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    body: RemediationChangeDecisionRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> BulkDecisionResponse:
    """Module 16 Phase 3: bulk-approve all pending changes in a remediation run.

    Only changes that have no existing decision are approved. Already-decided
    changes (approved or rejected) are skipped and counted in
    skipped_existing_count. No existing decision is ever modified or
    overwritten -- this endpoint is purely additive.

    Authentication: superuser only (same as per-change /approve).
    Tenant isolation: four-layer 404 chain via _get_remediation_run_or_404.
    Idempotent: calling twice is safe -- the second call returns
        approved_count=0, skipped_existing_count=total_changes.
    Performance: two SELECT queries + one bulk INSERT (db.add_all).
    """
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only superusers may approve remediation changes",
        )
    remediation_run, _ = _get_remediation_run_or_404(
        db, task_id, run_id, current_user.organization_id
    )
    return _bulk_decide(db, remediation_run, "approved", current_user, body)


@router.post(
    "/{task_id}/runs/{run_id}/remediation/reject-all",
    response_model=BulkDecisionResponse,
    status_code=status.HTTP_200_OK,
    tags=["tasks"],
)
def reject_all_remediation_changes(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    body: RemediationChangeDecisionRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> BulkDecisionResponse:
    """Module 16 Phase 3: bulk-reject all pending changes in a remediation run.

    Identical semantics and security model as approve_all_remediation_changes
    above -- see that endpoint's docstring. The only difference is
    decision_value='rejected'."""
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only superusers may reject remediation changes",
        )
    remediation_run, _ = _get_remediation_run_or_404(
        db, task_id, run_id, current_user.organization_id
    )
    return _bulk_decide(db, remediation_run, "rejected", current_user, body)


def _get_remediation_change_or_404(
    db: Session,
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    change_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> tuple[RemediationChange, RemediationRun]:
    """Four-layer 404 chain: task → task_run → remediation_run → change.
    Every level is scoped to organization_id so no cross-tenant data leaks
    through URL parameter manipulation. Returns (change, remediation_run).

    Uses the existing _get_remediation_run_or_404 for the first three
    layers -- if task_id, run_id, or organization_id don't match, that
    helper already raises 404 before we ever query remediation_changes.
    The fourth layer adds the change_id check inside the same org + run
    scope, consistent with how CleaningChange and RemediationChange list
    endpoints already scope sub-resource queries."""
    remediation_run, _ = _get_remediation_run_or_404(db, task_id, run_id, organization_id)
    change = db.execute(
        select(RemediationChange).where(
            RemediationChange.id == change_id,
            RemediationChange.remediation_run_id == remediation_run.id,
            RemediationChange.organization_id == organization_id,
        )
    ).scalar_one_or_none()
    if change is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Remediation change not found",
        )
    return change, remediation_run


def _get_existing_decision(
    db: Session,
    change_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> RemediationChangeDecision | None:
    """Return the latest decision for a change (by decision_timestamp DESC),
    or None if no decision has been recorded yet.  Used by both the GET
    endpoint (to return the current decision) and the POST endpoints (to
    enforce the Module 16 one-decision-per-change rule)."""
    return db.execute(
        select(RemediationChangeDecision)
        .where(
            RemediationChangeDecision.remediation_change_id == change_id,
            RemediationChangeDecision.organization_id == organization_id,
        )
        .order_by(RemediationChangeDecision.decision_timestamp.desc())
        .limit(1)
    ).scalar_one_or_none()


@router.get(
    "/{task_id}/runs/{run_id}/remediation/changes/{change_id}/decision",
    response_model=RemediationChangeDecisionRead,
    tags=["tasks"],
)
def get_remediation_change_decision(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    change_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> RemediationChangeDecision:
    """Module 16: return the current (latest by decision_timestamp) decision
    for a specific remediation change, or 404 if no decision exists yet.

    Authentication: any authenticated user in the owning organization.
    Authorization: read access to decisions is not gated on is_superuser --
    visibility of the approval outcome is organization-wide, same as how
    CleaningRun.status is readable by any org member regardless of who set
    it. Only *writing* (approve/reject) is superuser-restricted.

    Tenant isolation: the four-layer 404 chain in
    _get_remediation_change_or_404 ensures the change (and by extension
    the decision) belongs to current_user.organization_id before any data
    is returned. A change that exists but belongs to a different org is
    indistinguishable from one that doesn't exist."""
    change, _ = _get_remediation_change_or_404(
        db, task_id, run_id, change_id, current_user.organization_id
    )
    decision = _get_existing_decision(db, change.id, current_user.organization_id)
    if decision is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No decision has been recorded for this remediation change yet",
        )
    return decision


def _create_decision(
    db: Session,
    change: RemediationChange,
    decision_value: str,
    current_user: User,
    body: RemediationChangeDecisionRequest | None,
) -> RemediationChangeDecision:
    """Shared insert logic for the approve and reject endpoints.

    Security invariants enforced here (not in the caller):
    - reviewer_id / reviewer_name / reviewer_role / decision_timestamp are
      ALL set from server-side data (the authenticated user + utcnow).
      The caller's request body supplies only an optional comment -- there
      is no way to spoof reviewer identity or backdate a decision through
      the API.
    - Only superusers reach this function; the caller checks is_superuser
      and raises 403 before calling.
    - The 409 one-decision-per-change check is also done by the caller
      before reaching this function.

    reviewer_name snapshot: full_name if set, else email. Captures the
    display name at decision time so the audit trail survives a future
    name change or user deletion (reviewer_id goes NULL on delete, but
    reviewer_name / reviewer_role are immutable once written).

    reviewer_role snapshot: "superuser" for any user who reaches this
    function (enforced above). Stored as a string so a future role system
    can extend the vocabulary without a migration."""
    reviewer_name = current_user.full_name or current_user.email
    reviewer_role = "superuser"  # only superusers may create decisions in M16

    decision = RemediationChangeDecision(
        organization_id=change.organization_id,
        remediation_run_id=change.remediation_run_id,
        remediation_change_id=change.id,
        decision=decision_value,
        reviewer_id=current_user.id,
        reviewer_name=reviewer_name,
        reviewer_role=reviewer_role,
        decision_timestamp=datetime.now(timezone.utc),
        comment=body.comment if body else None,
        # applied_at / applied_by always NULL in Module 16
    )
    db.add(decision)
    db.commit()
    db.refresh(decision)
    return decision


@router.post(
    "/{task_id}/runs/{run_id}/remediation/changes/{change_id}/approve",
    response_model=RemediationChangeDecisionRead,
    status_code=status.HTTP_201_CREATED,
    tags=["tasks"],
)
def approve_remediation_change(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    change_id: uuid.UUID,
    body: RemediationChangeDecisionRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> RemediationChangeDecision:
    """Module 16: record an 'approved' decision for a specific remediation
    change proposal.  Returns the newly created decision row (HTTP 201).

    Authentication: authenticated user required (get_current_active_user).
    Authorization: superuser only -- regular org members may view change
      proposals (Module 15 endpoints) and decisions (GET /decision) but
      may not approve or reject them.
    Tenant isolation: four-layer 404 chain scopes every query to
      current_user.organization_id before any write occurs.

    409 Conflict: if any decision already exists for this change, the
      endpoint returns 409 and does NOT insert a new row.  Module 16 is
      an approval queue, not an override system.  Admin override is
      intentionally deferred to a future dedicated Override module where
      append-only history and explicit override reasons will be
      implemented safely (see design doc Section 2a).

    Request body: optional JSON object with a single optional field
      { "comment": "..." }. Omitting the body entirely is valid.
    Response: the immutable RemediationChangeDecision row as inserted --
      reviewer_id / reviewer_name / reviewer_role / decision_timestamp
      are always sourced from the server, never from the request body."""
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only superusers may approve remediation changes",
        )
    change, _ = _get_remediation_change_or_404(
        db, task_id, run_id, change_id, current_user.organization_id
    )
    existing = _get_existing_decision(db, change.id, current_user.organization_id)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A decision already exists for this remediation change "
                f"(decision: '{existing.decision}', "
                f"recorded at: {existing.decision_timestamp.isoformat()})"
            ),
        )
    return _create_decision(db, change, "approved", current_user, body)


@router.post(
    "/{task_id}/runs/{run_id}/remediation/changes/{change_id}/reject",
    response_model=RemediationChangeDecisionRead,
    status_code=status.HTTP_201_CREATED,
    tags=["tasks"],
)
def reject_remediation_change(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    change_id: uuid.UUID,
    body: RemediationChangeDecisionRequest | None = None,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> RemediationChangeDecision:
    """Module 16: record a 'rejected' decision for a specific remediation
    change proposal.  Returns the newly created decision row (HTTP 201).

    Identical security model and 409 semantics as approve_remediation_change
    above -- see that endpoint's docstring for the full rationale.  The only
    difference is decision_value='rejected'."""
    if not current_user.is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only superusers may reject remediation changes",
        )
    change, _ = _get_remediation_change_or_404(
        db, task_id, run_id, change_id, current_user.organization_id
    )
    existing = _get_existing_decision(db, change.id, current_user.organization_id)
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"A decision already exists for this remediation change "
                f"(decision: '{existing.decision}', "
                f"recorded at: {existing.decision_timestamp.isoformat()})"
            ),
        )
    return _create_decision(db, change, "rejected", current_user, body)


# ---------------------------------------------------------------------------
# Module 17 Phase 4: Validation API -- read-only
# ---------------------------------------------------------------------------


def _get_validation_run_or_404(
    db: Session, task_id: uuid.UUID, run_id: uuid.UUID, org_id: uuid.UUID
) -> tuple[ValidationRun, TaskRun]:
    """Shared 404 chain for every validation-result endpoint: task visible
    -> run visible -> validation result exists. Direct mirror of
    _get_remediation_run_or_404: also returns the TaskRun row itself (not
    just confirms its existence) so the summary endpoint can compute
    processing_duration_ms from TaskRun.started_at/finished_at without a
    second round-trip."""
    task = _get_active_task_or_404(db, task_id, org_id)
    task_run = db.execute(
        select(TaskRun).where(
            TaskRun.id == run_id,
            TaskRun.task_id == task.id,
            TaskRun.organization_id == org_id,
        )
    ).scalar_one_or_none()
    if task_run is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task run not found")

    validation_run = db.execute(
        select(ValidationRun).where(
            ValidationRun.task_run_id == run_id,
            ValidationRun.task_id == task.id,
            ValidationRun.organization_id == org_id,
        )
    ).scalar_one_or_none()
    if validation_run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Validation result not found"
        )
    return validation_run, task_run


@router.get("/{task_id}/runs/{run_id}/validation", response_model=ValidationRunRead)
def get_task_run_validation(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> ValidationRunRead:
    """Module 17 Phase 4: the summary result of a VALIDATE TaskRun --
    aggregate counts, results_by_rule breakdown, engine version, and
    processing duration. Cheap: every count was already computed by
    ValidationHandler at persist time (approved_changes_considered,
    passed_count, failed_count, skipped_count, results_by_rule) --
    no ValidationResult row is ever loaded in full and the pure engine is
    never re-run. processing_duration_ms is derived from TaskRun.started_at
    / TaskRun.finished_at (both always present on a completed run).

    Authentication: any authenticated active org member (no superuser gate
    -- read-only access to validation outcomes is organization-wide, same
    pattern as every other read-only result summary endpoint).
    Tenant isolation: three-layer 404 chain (task -> task_run ->
    validation_run), all queries scoped to current_user.organization_id."""
    validation_run, task_run = _get_validation_run_or_404(
        db, task_id, run_id, current_user.organization_id
    )

    processing_duration_ms = None
    if task_run.started_at is not None and task_run.finished_at is not None:
        processing_duration_ms = round(
            (task_run.finished_at - task_run.started_at).total_seconds() * 1000
        )

    return ValidationRunRead(
        id=validation_run.id,
        organization_id=validation_run.organization_id,
        task_run_id=validation_run.task_run_id,
        task_id=validation_run.task_id,
        data_source_id=validation_run.data_source_id,
        remediation_run_id=validation_run.remediation_run_id,
        approved_changes_considered=validation_run.approved_changes_considered,
        passed_count=validation_run.passed_count,
        failed_count=validation_run.failed_count,
        skipped_count=validation_run.skipped_count,
        results_by_rule=validation_run.results_by_rule,
        validation_engine_version=validation_run.validation_engine_version,
        processing_duration_ms=processing_duration_ms,
        created_at=validation_run.created_at,
    )


@router.get(
    "/{task_id}/runs/{run_id}/validation/results",
    response_model=PaginatedResponse[ValidationResultRead],
)
def list_task_run_validation_results(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    pagination: PaginationParams = Depends(),
    outcome: str | None = Query(default=None),
    validation_rule: str | None = Query(default=None),
    column_name: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> PaginatedResponse[ValidationResultRead]:
    """Module 17 Phase 4: paginated per-result list for a validation run,
    in stable (created_at ASC, id ASC) order. Filterable by outcome,
    validation_rule, and column_name (exact match), individually or
    combined.

    outcome must be one of VALIDATION_OUTCOMES ('passed', 'failed',
    'skipped'); an unrecognised value is rejected with 422.
    validation_rule must be one of VALIDATION_RULE_NAMES; an unrecognised
    value is rejected with 422.
    column_name is an exact-match filter against RemediationChange.column_name
    using a single explicit JOIN -- only introduced when column_name is
    provided, avoiding an unnecessary join on unfiltered requests. This
    is the only query in this endpoint that touches RemediationChange.

    Performance: one paginated data query + one COUNT query with identical
    filters; no per-row sub-queries; no N+1 pattern.

    Default limit: 50. Maximum limit: 100 (enforced by PaginationParams).
    Authentication: any authenticated active org member.
    Tenant isolation: all filters include ValidationResult.organization_id."""
    validation_run, _ = _get_validation_run_or_404(
        db, task_id, run_id, current_user.organization_id
    )

    if outcome is not None and outcome not in VALIDATION_OUTCOMES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"outcome must be one of {VALIDATION_OUTCOMES}",
        )
    if validation_rule is not None and validation_rule not in VALIDATION_RULE_NAMES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"validation_rule must be one of {VALIDATION_RULE_NAMES}",
        )

    # Base filters -- always org-scoped and run-scoped.
    filters: list = [
        ValidationResult.validation_run_id == validation_run.id,
        ValidationResult.organization_id == current_user.organization_id,
    ]
    if outcome is not None:
        filters.append(ValidationResult.outcome == outcome)
    if validation_rule is not None:
        filters.append(ValidationResult.validation_rule == validation_rule)

    # column_name lives on RemediationChange, not ValidationResult.
    # Introduce the JOIN only when this filter is requested to keep the
    # common unfiltered path join-free.
    if column_name is not None:
        base_stmt = (
            select(ValidationResult)
            .join(
                RemediationChange,
                ValidationResult.remediation_change_id == RemediationChange.id,
            )
            .where(*filters, RemediationChange.column_name == column_name)
        )
        count_stmt = (
            select(func.count())
            .select_from(ValidationResult)
            .join(
                RemediationChange,
                ValidationResult.remediation_change_id == RemediationChange.id,
            )
            .where(*filters, RemediationChange.column_name == column_name)
        )
    else:
        base_stmt = select(ValidationResult).where(*filters)
        count_stmt = select(func.count()).select_from(ValidationResult).where(*filters)

    total = db.execute(count_stmt).scalar_one()
    rows = db.execute(
        base_stmt
        .order_by(ValidationResult.created_at, ValidationResult.id)
        .limit(pagination.limit)
        .offset(pagination.offset)
    ).scalars().all()

    items = [
        ValidationResultRead(
            id=row.id,
            validation_run_id=row.validation_run_id,
            remediation_run_id=row.remediation_run_id,
            remediation_change_id=row.remediation_change_id,
            source_issue_id=row.source_issue_id,
            validation_rule=row.validation_rule,
            validation_rule_version=row.validation_rule_version,
            outcome=row.outcome,
            reason=row.reason,
            original_value=row.original_value,
            proposed_value=row.proposed_value,
            validation_engine_version=row.validation_engine_version,
            created_at=row.created_at,
        )
        for row in rows
    ]

    return PaginatedResponse(
        items=items, total=total, limit=pagination.limit, offset=pagination.offset
    )


# ── Module 18: Quality Control read-only endpoints ──────────────────────────


def _get_quality_control_run_or_404(
    db: Session, task_id: uuid.UUID, run_id: uuid.UUID, org_id: uuid.UUID
) -> tuple[QualityControlRun, TaskRun]:
    """Shared 404 chain: task visible → run visible → qc result exists.
    Returns (QualityControlRun, TaskRun) so the summary endpoint can derive
    processing_duration_ms without a second query."""
    task = _get_active_task_or_404(db, task_id, org_id)
    task_run = db.execute(
        select(TaskRun).where(
            TaskRun.id == run_id,
            TaskRun.task_id == task.id,
            TaskRun.organization_id == org_id,
        )
    ).scalar_one_or_none()
    if task_run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Task run not found"
        )

    qc_run = db.execute(
        select(QualityControlRun).where(
            QualityControlRun.task_run_id == run_id,
            QualityControlRun.task_id == task.id,
            QualityControlRun.organization_id == org_id,
        )
    ).scalar_one_or_none()
    if qc_run is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Quality control result not found",
        )
    return qc_run, task_run


@router.get(
    "/{task_id}/runs/{run_id}/quality-control",
    response_model=QualityControlRunRead,
)
def get_task_run_quality_control(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> QualityControlRunRead:
    """Module 18 Phase 3: quality control summary for a QUALITY_CTRL TaskRun.

    Returns overall score, release recommendation, category breakdowns, and
    processing duration. All counts and scores were pre-computed by
    QualityControlHandler — no aggregation at query time.
    processing_duration_ms is derived from TaskRun.started_at/finished_at.
    Tenant isolation: three-layer 404 chain, all queries org-scoped."""
    qc_run, task_run = _get_quality_control_run_or_404(
        db, task_id, run_id, current_user.organization_id
    )

    processing_duration_ms: int | None = None
    if task_run.started_at is not None and task_run.finished_at is not None:
        processing_duration_ms = round(
            (task_run.finished_at - task_run.started_at).total_seconds() * 1000
        )

    return QualityControlRunRead(
        id=qc_run.id,
        organization_id=qc_run.organization_id,
        task_run_id=qc_run.task_run_id,
        task_id=qc_run.task_id,
        data_source_id=qc_run.data_source_id,
        validation_run_id=qc_run.validation_run_id,
        remediation_run_id=qc_run.remediation_run_id,
        issue_detection_run_id=qc_run.issue_detection_run_id,
        data_profile_id=qc_run.data_profile_id,
        quality_engine_version=qc_run.quality_engine_version,
        overall_score=qc_run.overall_score,
        release_recommendation=qc_run.release_recommendation,
        total_findings=qc_run.total_findings,
        blocking_count=qc_run.blocking_count,
        warning_count=qc_run.warning_count,
        info_count=qc_run.info_count,
        category_scores=qc_run.category_scores,
        category_statuses=qc_run.category_statuses,
        category_weights_used=qc_run.category_weights_used,
        post_remediation_stats=qc_run.post_remediation_stats,
        processing_duration_ms=processing_duration_ms,
        created_at=qc_run.created_at,
    )


@router.get(
    "/{task_id}/runs/{run_id}/quality-control/findings",
    response_model=PaginatedResponse[QualityFindingRead],
)
def list_task_run_quality_control_findings(
    task_id: uuid.UUID,
    run_id: uuid.UUID,
    pagination: PaginationParams = Depends(),
    category: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    outcome: str | None = Query(default=None),
    rule_name: str | None = Query(default=None),
    affected_column: str | None = Query(default=None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> PaginatedResponse[QualityFindingRead]:
    """Module 18 Phase 3: paginated per-finding list for a quality control run.

    Stable order: created_at ASC, id ASC. Filters (all exact-match, AND):
    - category: validated against QUALITY_CATEGORIES; 422 if unknown
    - severity: validated against QUALITY_FINDING_SEVERITIES; 422 if unknown
    - outcome: validated against QUALITY_FINDING_OUTCOMES; 422 if unknown
    - rule_name, affected_column: unknown value → empty result (not 422)

    Default limit: 50, max 100. Tenant isolation: org-scoped on every query."""
    qc_run, _ = _get_quality_control_run_or_404(
        db, task_id, run_id, current_user.organization_id
    )

    if category is not None and category not in QUALITY_CATEGORIES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"category must be one of {sorted(QUALITY_CATEGORIES)}",
        )
    if severity is not None and severity not in QUALITY_FINDING_SEVERITIES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"severity must be one of {sorted(QUALITY_FINDING_SEVERITIES)}",
        )
    if outcome is not None and outcome not in QUALITY_FINDING_OUTCOMES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"outcome must be one of {sorted(QUALITY_FINDING_OUTCOMES)}",
        )

    filters: list = [
        QualityFinding.quality_control_run_id == qc_run.id,
        QualityFinding.organization_id == current_user.organization_id,
    ]
    if category is not None:
        filters.append(QualityFinding.category == category)
    if severity is not None:
        filters.append(QualityFinding.severity == severity)
    if outcome is not None:
        filters.append(QualityFinding.outcome == outcome)
    if rule_name is not None:
        filters.append(QualityFinding.rule_name == rule_name)
    if affected_column is not None:
        filters.append(QualityFinding.affected_column == affected_column)

    base_stmt = select(QualityFinding).where(*filters)
    count_stmt = select(func.count()).select_from(QualityFinding).where(*filters)

    total = db.execute(count_stmt).scalar_one()
    rows = (
        db.execute(
            base_stmt
            .order_by(QualityFinding.created_at, QualityFinding.id)
            .limit(pagination.limit)
            .offset(pagination.offset)
        )
        .scalars()
        .all()
    )

    items = [
        QualityFindingRead(
            id=row.id,
            quality_control_run_id=row.quality_control_run_id,
            category=row.category,
            rule_name=row.rule_name,
            rule_version=row.rule_version,
            severity=row.severity,
            outcome=row.outcome,
            reason=row.reason,
            affected_row_count=row.affected_row_count,
            affected_column=row.affected_column,
            source_issue_id=row.source_issue_id,
            remediation_change_id=row.remediation_change_id,
            validation_result_id=row.validation_result_id,
            quality_engine_version=row.quality_engine_version,
            created_at=row.created_at,
        )
        for row in rows
    ]

    return PaginatedResponse(
        items=items, total=total, limit=pagination.limit, offset=pagination.offset
    )
