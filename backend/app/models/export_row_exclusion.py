"""
ExportRowExclusion: one row per record EXCLUDED from an ExportRun's
output file -- the one new per-row audit question Module 9 introduces
that no Module 6/7/8 audit table answers ("why is this row missing from
my exported file", as opposed to "how was this row transformed" or "how
was this row grouped"). Capped per run at
EXPORT_MAX_PERSISTED_EXCLUSIONS, same bounded-but-never-silent pattern as
CleaningChange/StandardizationChange/MatchDecision --
ExportRun.excluded_row_count is always the true total even when this is
capped. Append-only, never updated after creation (including after
ExportRun rollback). See
docs/module-9-data-export-engine-design.md Section 7.

Deliberately does NOT re-explain *why* a row was grouped with its
canonical -- that question is already answered by Module 8's own
MatchDecision audit trail (cross-referenced here via match_group_id).
This table only answers "which rows are missing, and which group
absorbed them."
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKeyConstraint,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class ExportRowExclusion(Base):
    __tablename__ = "export_row_exclusions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "export_run_id"],
            ["export_runs.organization_id", "export_runs.id"],
            name="fk_export_row_exclusions_org_export_run",
            ondelete="CASCADE",
        ),
        # The Module 8 MatchGroup this row was folded into -- the first
        # cross-module FK in this project's history into a table owned by
        # a non-immediate-parent module (see design doc Section 19).
        ForeignKeyConstraint(
            ["organization_id", "match_group_id"],
            ["match_groups.organization_id", "match_groups.id"],
            name="fk_export_row_exclusions_org_match_group",
            ondelete="RESTRICT",
        ),
        # A given row is excluded at most once per export run.
        UniqueConstraint(
            "export_run_id", "row_index", name="uq_export_row_exclusions_run_row"
        ),
        CheckConstraint("row_index >= 0", name="ck_export_row_exclusions_row_index_nonnegative"),
        CheckConstraint(
            "canonical_row_index >= 0",
            name="ck_export_row_exclusions_canonical_row_index_nonnegative",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    export_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)

    row_index: Mapped[int] = mapped_column(Integer(), nullable=False)
    # Never NULL -- a row is only ever excluded because it belonged to a
    # duplicate group (Section 7).
    match_group_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    # Denormalized from that MatchGroup for convenience, so a caller
    # doesn't need a second lookup to see which row was kept instead.
    canonical_row_index: Mapped[int] = mapped_column(Integer(), nullable=False)
    reason: Mapped[str] = mapped_column(Text(), nullable=False)
    # EXPORT_ENGINE_VERSION in effect when this row was produced.
    rule_version: Mapped[str] = mapped_column(String(20), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    export_run: Mapped["ExportRun"] = relationship(back_populates="exclusions")  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"ExportRowExclusion(id={self.id!r}, export_run={self.export_run_id!r}, "
            f"row_index={self.row_index!r})"
        )
