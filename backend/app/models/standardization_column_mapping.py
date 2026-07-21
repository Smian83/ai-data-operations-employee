"""
StandardizationColumnMapping: organization-configured override of the
built-in header-name classification heuristic (see
app.standardization.classification) -- lets an organization explicitly
declare that a column is a given field_type, scoped either to one
data_source or (when data_source_id is NULL) to every data source in
that organization. See
docs/module-7-data-standardization-engine-design.md Section 3.

Same tenant-scoped, soft-deletable, case-insensitive-unique-name
convention every named resource since Module 3 uses (see DataSource,
Task) -- the only wrinkle is that the uniqueness key here has an
optional (nullable) data_source_id component, which a single partial
unique index cannot correctly express (NULL != NULL under standard SQL
uniqueness semantics, so two "applies org-wide" rows for the same column
would NOT collide under one plain unique index). Two partial indexes are
used instead: one for the data-source-scoped case, one for the org-wide
case -- each internally consistent, together closing the gap a single
index would leave open.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import STANDARDIZATION_FIELD_TYPES


class StandardizationColumnMapping(Base):
    __tablename__ = "standardization_column_mappings"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_standardization_column_mappings_org_data_source",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_standardization_column_mappings_scoped_active",
            "organization_id",
            "data_source_id",
            text("lower(trim(column_name))"),
            unique=True,
            postgresql_where=text("is_active = true AND data_source_id IS NOT NULL"),
            sqlite_where=text("is_active = 1 AND data_source_id IS NOT NULL"),
        ),
        Index(
            "ix_standardization_column_mappings_orgwide_active",
            "organization_id",
            text("lower(trim(column_name))"),
            unique=True,
            postgresql_where=text("is_active = true AND data_source_id IS NULL"),
            sqlite_where=text("is_active = 1 AND data_source_id IS NULL"),
        ),
        CheckConstraint(
            "field_type IN (" + ", ".join(f"'{t}'" for t in STANDARDIZATION_FIELD_TYPES) + ")",
            name="ck_standardization_column_mappings_field_type_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # NULL = applies to every data source in this organization.
    data_source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    column_name: Mapped[str] = mapped_column(String(255), nullable=False)
    field_type: Mapped[str] = mapped_column(String(30), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"StandardizationColumnMapping(id={self.id!r}, org={self.organization_id!r}, "
            f"column={self.column_name!r}, field_type={self.field_type!r})"
        )
