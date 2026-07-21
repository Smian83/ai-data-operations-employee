"""
StandardizationLookupEntry: organization-supplied lookup-table entries
(abbreviation expansions, canonical company-name suffixes, country-name
variants, etc.) that the rule engine consults ahead of its built-in
defaults for the same key. field_type=NULL means the entry applies
across any classified field (the generic "common abbreviations" pass
described in the design doc Section 6); a non-NULL field_type scopes the
entry to that field type's own lookup consultation (e.g. company-name
suffix canonicalization only consults field_type='company_name' entries).
See docs/module-7-data-standardization-engine-design.md Section 3.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import STANDARDIZATION_FIELD_TYPES


class StandardizationLookupEntry(Base):
    __tablename__ = "standardization_lookup_entries"
    __table_args__ = (
        # Two partial indexes for the same reason StandardizationColumnMapping
        # needs two: field_type is nullable ("applies across field types"),
        # and NULL != NULL means a single plain unique index would let
        # duplicate NULL-scoped entries for the same key through silently.
        Index(
            "ix_standardization_lookup_entries_scoped_active",
            "organization_id",
            "field_type",
            text("lower(trim(lookup_key))"),
            unique=True,
            postgresql_where=text("is_active = true AND field_type IS NOT NULL"),
            sqlite_where=text("is_active = 1 AND field_type IS NOT NULL"),
        ),
        Index(
            "ix_standardization_lookup_entries_global_active",
            "organization_id",
            text("lower(trim(lookup_key))"),
            unique=True,
            postgresql_where=text("is_active = true AND field_type IS NULL"),
            sqlite_where=text("is_active = 1 AND field_type IS NULL"),
        ),
        CheckConstraint(
            "field_type IS NULL OR field_type IN ("
            + ", ".join(f"'{t}'" for t in STANDARDIZATION_FIELD_TYPES)
            + ")",
            name="ck_standardization_lookup_entries_field_type_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # NULL = applies across any classified field (generic abbreviations).
    field_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    lookup_key: Mapped[str] = mapped_column(String(255), nullable=False)
    lookup_value: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"StandardizationLookupEntry(id={self.id!r}, org={self.organization_id!r}, "
            f"field_type={self.field_type!r}, key={self.lookup_key!r})"
        )
