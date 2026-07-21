"""
MatchGroup: one row per duplicate cluster produced by Module 8's union-
find clustering step (Section 6 of
docs/module-8-data-matching-deduplication-design.md). Append-only, never
updated after creation (including after MatchRun rollback -- Section 10).
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Integer,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class MatchGroup(Base):
    __tablename__ = "match_groups"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "match_run_id"],
            ["match_runs.organization_id", "match_runs.id"],
            name="fk_match_groups_org_match_run",
            ondelete="CASCADE",
        ),
        # Required so MatchDecision.match_group_id can have a tenant-aware
        # composite FK -> (organization_id, id), same pattern as every
        # other Module 6/7/8 run-child table.
        UniqueConstraint("organization_id", "id", name="uq_match_groups_org_id"),
        CheckConstraint("record_count >= 2", name="ck_match_groups_record_count_min"),
        CheckConstraint(
            "confidence_score >= 0 AND confidence_score <= 1",
            name="ck_match_groups_confidence_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    match_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)

    # The row_index, within this run's loaded standardized CSV, of the
    # deterministically-selected canonical record (Section 8: lowest
    # row_index in the group, fixed, not organization-configurable).
    canonical_row_index: Mapped[int] = mapped_column(Integer(), nullable=False)
    record_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    # Minimum confidence across this group's member 'duplicate' decisions.
    confidence_score: Mapped[float] = mapped_column(Float(), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    match_run: Mapped["MatchRun"] = relationship(back_populates="groups")  # noqa: F821
    # overlaps: organization_id is written by both this relationship
    # (via match_groups) and MatchRun.decisions (via match_runs) for the
    # same MatchDecision rows -- both composite FKs are intentional
    # (Section 3/9), so the overlap is expected, not a modeling mistake.
    decisions: Mapped[list["MatchDecision"]] = relationship(  # noqa: F821
        back_populates="match_group", overlaps="decisions,match_run"
    )

    def __repr__(self) -> str:
        return (
            f"MatchGroup(id={self.id!r}, match_run={self.match_run_id!r}, "
            f"canonical_row_index={self.canonical_row_index!r}, "
            f"record_count={self.record_count!r})"
        )
