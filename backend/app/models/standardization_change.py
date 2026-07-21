"""
StandardizationChange: append-only per-cell audit record for Module 7.
Direct structural mirror of CleaningChange (see models/cleaning_change.py),
plus two fields CleaningChange didn't need -- field_type (Module 7 rules
are classified per field type, not per raw data type) and rule_version
(every individual change, not just the parent run, stays attributable to
the exact engine version that produced it). See
docs/module-7-data-standardization-engine-design.md Sections 3, 7.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class StandardizationChange(Base):
    __tablename__ = "standardization_changes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "standardization_run_id"],
            ["standardization_runs.organization_id", "standardization_runs.id"],
            name="fk_standardization_changes_org_standardization_run",
            ondelete="CASCADE",
        ),
        CheckConstraint("row_index >= 0", name="ck_standardization_changes_row_index_nonnegative"),
        CheckConstraint(
            "confidence_score >= 0 AND confidence_score <= 1",
            name="ck_standardization_changes_confidence_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    standardization_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)

    row_index: Mapped[int] = mapped_column(Integer(), nullable=False)
    column_name: Mapped[str] = mapped_column(String(255), nullable=False)
    field_type: Mapped[str] = mapped_column(String(30), nullable=False)
    original_value: Mapped[str] = mapped_column(Text(), nullable=False)
    standardized_value: Mapped[str] = mapped_column(Text(), nullable=False)
    rule_name: Mapped[str] = mapped_column(String(50), nullable=False)
    # The STANDARDIZATION_ENGINE_VERSION in effect when THIS row was
    # produced -- recorded per-change, not only once on the parent run.
    rule_version: Mapped[str] = mapped_column(String(20), nullable=False)
    reason: Mapped[str] = mapped_column(Text(), nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float(), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    standardization_run: Mapped["StandardizationRun"] = relationship(  # noqa: F821
        back_populates="changes"
    )

    def __repr__(self) -> str:
        return (
            f"StandardizationChange(id={self.id!r}, "
            f"standardization_run={self.standardization_run_id!r}, "
            f"column={self.column_name!r})"
        )
