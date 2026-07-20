"""Immutable profiling result produced for one successful CSV TaskRun."""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class DataProfile(Base):
    __tablename__ = "data_profiles"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_data_profiles_org_task_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_data_profiles_org_task",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_data_profiles_org_data_source",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("task_run_id", name="uq_data_profiles_task_run_id"),
        CheckConstraint("source_size_bytes >= 0", name="ck_data_profiles_source_size_nonnegative"),
        CheckConstraint("row_count >= 0", name="ck_data_profiles_row_count_nonnegative"),
        CheckConstraint("column_count > 0", name="ck_data_profiles_column_count_positive"),
        CheckConstraint(
            "duplicate_row_count >= 0 AND duplicate_row_count <= row_count",
            name="ck_data_profiles_duplicate_count_valid",
        ),
        CheckConstraint(
            "missing_value_total >= 0",
            name="ck_data_profiles_missing_total_nonnegative",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    task_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    data_source_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)

    source_filename: Mapped[str] = mapped_column(String(1024), nullable=False)
    source_size_bytes: Mapped[int] = mapped_column(Integer(), nullable=False)
    source_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    detected_encoding: Mapped[str] = mapped_column(String(50), nullable=False)
    delimiter: Mapped[str] = mapped_column(String(8), nullable=False)
    row_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    column_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    duplicate_row_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    missing_value_total: Mapped[int] = mapped_column(Integer(), nullable=False)
    column_profiles: Mapped[list] = mapped_column(JSON, nullable=False)
    structural_issues: Mapped[list] = mapped_column(JSON, nullable=False)
    limits_applied: Mapped[dict] = mapped_column(JSON, nullable=False)
    profiled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    task_run: Mapped["TaskRun"] = relationship(back_populates="data_profile")  # noqa: F821
