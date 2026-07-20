"""
DataSource: an external system an organization wants the AI agent to operate
on. Module 3 only models and CRUDs this — actually connecting to it is
Module 4's job.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    String,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
# NOTE: this must be sqlalchemy.dialects.postgresql.ENUM, NOT the generic
# sqlalchemy.Enum. The generic Enum silently ignores an unrecognized
# create_type kwarg (its __init__ never pops it), so create_type=False
# below would otherwise have no effect and Base.metadata.create_all()
# would independently emit CREATE TYPE against a live PostgreSQL database
# -- exactly the DuplicateObject bug hit during Module 4 verification.
# Only postgresql.ENUM genuinely implements and honors create_type.
from sqlalchemy.dialects.postgresql import ENUM as SAEnum
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import SourceType

# create_type=False: Alembic migrations (database/alembic/versions/
# e8e9044941dd_*.py) are the SOLE owner of this native PostgreSQL type's
# lifecycle -- CREATE/DROP TYPE only ever happens there. Without this,
# SQLAlchemy's default create_type=True means Base.metadata.create_all()
# (used by tests/conftest.py against a live database) would independently
# issue its own CREATE TYPE for this exact type name, outside Alembic's
# tracking entirely -- a real "two owners" bug that surfaced as a
# PostgreSQL DuplicateObject error during Module 4's real-Postgres
# verification. create_constraint=True is unrelated and unaffected -- it
# only governs the SQLite CHECK-constraint fallback (SQLite has no native
# enum type at all), which this model must still provide.
source_type_enum = SAEnum(
    SourceType,
    name="source_type_enum",
    values_callable=lambda obj: [e.value for e in obj],
    create_constraint=True,
    create_type=False,
)


class DataSource(Base):
    __tablename__ = "data_sources"
    __table_args__ = (
        # Required so Task can have a composite FK (organization_id,
        # data_source_id) -> (organization_id, id): Postgres requires a
        # unique/PK constraint on the exact column pair being referenced.
        # This is logically redundant (id alone is already unique) but is
        # the standard pattern for tenant-scoped composite foreign keys.
        UniqueConstraint("organization_id", "id", name="uq_data_sources_org_id"),
        # Case-insensitive, whitespace-trimmed uniqueness, scoped to ACTIVE
        # rows only — soft-deleting a data source frees its name for reuse.
        Index(
            "ix_data_sources_org_name_active",
            "organization_id",
            text("lower(trim(name))"),
            unique=True,
            postgresql_where=text("is_active = true"),
            sqlite_where=text("is_active = 1"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    source_type: Mapped[SourceType] = mapped_column(source_type_enum, nullable=False)
    # Non-secret connection metadata only (e.g. host, port, database name).
    # Real credentials belong to a future encrypted secrets module — the API
    # layer rejects secret-shaped keys before this column is ever written.
    connection_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    tasks: Mapped[list["Task"]] = relationship(back_populates="data_source")  # noqa: F821

    def __repr__(self) -> str:
        return f"DataSource(id={self.id!r}, org={self.organization_id!r}, name={self.name!r})"
