"""
CleaningChange: append-only, immutable per-cell change record produced by a
CleaningRun. Never updated or deleted, including after rollback -- see
docs/module-6-data-cleaning-engine-design.md Section 11. Persisted rows are
capped at settings.cleaning_max_persisted_changes per run (see
app.worker.handlers.cleaning); CleaningRun.total_changes_count is always
the true total even when individual rows are capped.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class CleaningChange(Base):
    __tablename__ = "cleaning_changes"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "cleaning_run_id"],
            ["cleaning_runs.organization_id", "cleaning_runs.id"],
            name="fk_cleaning_changes_org_cleaning_run",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    cleaning_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)

    row_index: Mapped[int] = mapped_column(Integer(), nullable=False)
    column_name: Mapped[str] = mapped_column(String(255), nullable=False)
    original_value: Mapped[str] = mapped_column(Text(), nullable=False)
    cleaned_value: Mapped[str] = mapped_column(Text(), nullable=False)
    rule_name: Mapped[str] = mapped_column(String(50), nullable=False)
    reason: Mapped[str] = mapped_column(Text(), nullable=False)
    confidence_score: Mapped[float] = mapped_column(Float(), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    cleaning_run: Mapped["CleaningRun"] = relationship(back_populates="changes")

    def __repr__(self) -> str:
        return (
            f"CleaningChange(cleaning_run={self.cleaning_run_id!r}, "
            f"row={self.row_index!r}, column={self.column_name!r}, "
            f"rule={self.rule_name!r})"
        )
