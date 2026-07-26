"""
RemediationDatasetConfig: organization-configured, per-data-source toggle
for the two whole-row removal proposals (remove_duplicate_row /
remove_duplicate_primary_key). Deliberately a SEPARATE table from
RemediationColumnRule -- duplicate removal is a whole-dataset decision,
not a per-column one, so folding it into the column-scoped table would
overload that table's unique-index semantics with a second, unrelated
nullability dimension. See
docs/module-15-deterministic-cleaning-engine-design.md Section 3.

Unlike RemediationColumnRule, data_source_id here is REQUIRED (no
org-wide fallback): removal is consequential enough that it must be
opted into per data source explicitly, never inherited from a blanket
organization default. Absence of an active row for a given data_source_id
means both flags are False -- no removal proposal is ever generated.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class RemediationDatasetConfig(Base):
    __tablename__ = "remediation_dataset_configs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_remediation_dataset_configs_org_data_source",
            ondelete="RESTRICT",
        ),
        # At most one ACTIVE config row per data source -- a partial
        # unique index (not a plain UniqueConstraint) so a deactivated row
        # can be superseded by a new active one without violating
        # uniqueness, the same soft-delete-then-recreate convention every
        # other organization-configuration table in this project follows.
        Index(
            "ix_remediation_dataset_configs_active",
            "organization_id",
            "data_source_id",
            unique=True,
            postgresql_where=text("is_active = true"),
            sqlite_where=text("is_active = 1"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    data_source_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)

    remove_duplicate_rows_enabled: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False
    )
    remove_duplicate_primary_keys_enabled: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False
    )

    is_active: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"RemediationDatasetConfig(id={self.id!r}, org={self.organization_id!r}, "
            f"data_source={self.data_source_id!r})"
        )
