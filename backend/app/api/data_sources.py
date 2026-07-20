"""
Data Source CRUD, tenant-scoped.

Every query filters by organization_id == current_user.organization_id.
Inactive resources behave exactly like non-existent ones (404) for any
direct operation on their own id — see README "Data Sources & Tasks" for
the full rationale.
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import PaginationParams, get_current_active_user
from app.db.session import get_db
from app.models.data_source import DataSource
from app.models.user import User
from app.schemas.data_source import DataSourceCreate, DataSourceRead, DataSourceUpdate
from app.schemas.pagination import PaginatedResponse

router = APIRouter(prefix="/data-sources", tags=["data-sources"])


def _name_taken(db: Session, org_id: uuid.UUID, name: str, exclude_id: uuid.UUID | None = None) -> bool:
    stmt = select(DataSource.id).where(
        DataSource.organization_id == org_id,
        func.lower(func.trim(DataSource.name)) == name.strip().lower(),
        DataSource.is_active.is_(True),
    )
    if exclude_id is not None:
        stmt = stmt.where(DataSource.id != exclude_id)
    return db.execute(stmt).scalar_one_or_none() is not None


def _get_active_or_404(db: Session, data_source_id: uuid.UUID, org_id: uuid.UUID) -> DataSource:
    data_source = db.execute(
        select(DataSource).where(
            DataSource.id == data_source_id,
            DataSource.organization_id == org_id,
            DataSource.is_active.is_(True),
        )
    ).scalar_one_or_none()
    if data_source is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Data source not found")
    return data_source


@router.post("", response_model=DataSourceRead, status_code=status.HTTP_201_CREATED)
def create_data_source(
    payload: DataSourceCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> DataSource:
    if _name_taken(db, current_user.organization_id, payload.name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A data source named '{payload.name}' already exists",
        )

    data_source = DataSource(
        organization_id=current_user.organization_id,
        name=payload.name,
        source_type=payload.source_type,
        connection_metadata=payload.connection_metadata,
        created_by=current_user.id,
    )
    db.add(data_source)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A data source named '{payload.name}' already exists",
        )
    db.refresh(data_source)
    return data_source


@router.get("", response_model=PaginatedResponse[DataSourceRead])
def list_data_sources(
    pagination: PaginationParams = Depends(),
    include_inactive: bool = Query(default=False),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> PaginatedResponse[DataSourceRead]:
    filters = [DataSource.organization_id == current_user.organization_id]
    if not include_inactive:
        filters.append(DataSource.is_active.is_(True))

    total = db.execute(
        select(func.count()).select_from(DataSource).where(*filters)
    ).scalar_one()

    rows = db.execute(
        select(DataSource)
        .where(*filters)
        .order_by(DataSource.created_at.desc())
        .limit(pagination.limit)
        .offset(pagination.offset)
    ).scalars().all()

    return PaginatedResponse(
        items=list(rows), total=total, limit=pagination.limit, offset=pagination.offset
    )


@router.get("/{data_source_id}", response_model=DataSourceRead)
def get_data_source(
    data_source_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> DataSource:
    return _get_active_or_404(db, data_source_id, current_user.organization_id)


@router.patch("/{data_source_id}", response_model=DataSourceRead)
def update_data_source(
    data_source_id: uuid.UUID,
    payload: DataSourceUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> DataSource:
    data_source = _get_active_or_404(db, data_source_id, current_user.organization_id)

    if payload.name is not None and payload.name.lower() != data_source.name.strip().lower():
        if _name_taken(db, current_user.organization_id, payload.name, exclude_id=data_source.id):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A data source named '{payload.name}' already exists",
            )
        data_source.name = payload.name

    if payload.source_type is not None:
        data_source.source_type = payload.source_type
    if payload.connection_metadata is not None:
        data_source.connection_metadata = payload.connection_metadata

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A data source named '{payload.name}' already exists",
        )
    db.refresh(data_source)
    return data_source


@router.delete("/{data_source_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_data_source(
    data_source_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> None:
    data_source = _get_active_or_404(db, data_source_id, current_user.organization_id)
    data_source.is_active = False
    db.commit()
