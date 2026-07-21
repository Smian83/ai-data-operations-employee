"""
MatchSkippedBlock (new in the approved design revision): one row per
block skipped for exceeding MATCH_MAX_BLOCK_SIZE. Closes the "why were
these two records never compared" gap MatchDecision alone cannot answer,
while remaining bounded, deterministic, and tenant-isolated -- see
docs/module-8-data-matching-deduplication-design.md Section 3/6/11.

Bounded on both axes without a separate per-run cap: the number of rows
for one run can never exceed row_count / MATCH_MAX_BLOCK_SIZE (a block is
only ever recorded here if it was too large to compare), and each row's
sample_row_indices array is itself capped at MATCH_MAX_SKIPPED_ROW_SAMPLE.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
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


class MatchSkippedBlock(Base):
    __tablename__ = "match_skipped_blocks"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "match_run_id"],
            ["match_runs.organization_id", "match_runs.id"],
            name="fk_match_skipped_blocks_org_run",
            ondelete="CASCADE",
        ),
        # A given block is only ever skipped once per run -- this also
        # protects idempotency (see the model docstring / design Section
        # 3): a retried MATCH TaskRun never creates a second MatchRun row
        # at all (parent handler short-circuits first), so this
        # constraint is a second, defense-in-depth guarantee, not the
        # primary one.
        UniqueConstraint(
            "match_run_id", "blocking_key", name="uq_match_skipped_blocks_run_key"
        ),
        CheckConstraint("block_size > 0", name="ck_match_skipped_blocks_size_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    match_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)

    blocking_key: Mapped[str] = mapped_column(String(500), nullable=False)
    # True, full count of rows sharing this blocking-key value -- never
    # truncated or estimated, even though not every row is individually
    # listed below.
    block_size: Mapped[int] = mapped_column(Integer(), nullable=False)
    # Small, deterministic sample of this block's row_index values
    # (lowest MATCH_MAX_SKIPPED_ROW_SAMPLE), for spot-checking without
    # persisting the full (potentially very large) row list.
    sample_row_indices: Mapped[list] = mapped_column(JSON, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    match_run: Mapped["MatchRun"] = relationship(back_populates="skipped_blocks")  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"MatchSkippedBlock(id={self.id!r}, match_run={self.match_run_id!r}, "
            f"blocking_key={self.blocking_key!r}, block_size={self.block_size!r})"
        )
