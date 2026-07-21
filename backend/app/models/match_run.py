"""
MatchRun: one row per Module 8 MATCH TaskRun -- immutable core fields
(what was matched, aggregate counts, engine version) plus mutable
approval-state fields, direct structural mirror of CleaningRun/
StandardizationRun's one-row-per-run idempotency pattern (see
models/standardization_run.py). Unlike CleaningRun/StandardizationRun,
there are NO output_file_path/output_sha256 columns -- Module 8 produces
no output file at all (see
docs/module-8-data-matching-deduplication-design.md Section 2's
architectural decision). See design doc Sections 3, 9, 10.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
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
from app.models.enums import MATCH_RUN_STATUSES


class MatchRun(Base):
    __tablename__ = "match_runs"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_match_runs_org_task_run",
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_match_runs_org_task",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_match_runs_org_data_source",
            ondelete="RESTRICT",
        ),
        # The STANDARDIZE run whose approved StandardizationRun this match
        # run was produced from -- denormalized from TaskRun.source_task_
        # run_id, same rationale as StandardizationRun.source_task_run_id.
        ForeignKeyConstraint(
            ["organization_id", "source_task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_match_runs_org_source_task_run",
            ondelete="RESTRICT",
        ),
        # Nullable -- NULL means no active MatchRuleSet existed for this
        # organization/data source at run time, so only the always-
        # available exact-duplicate pass (Stage 1) ran (design doc
        # Section 4 step 3, Section 7).
        ForeignKeyConstraint(
            ["organization_id", "rule_set_id"],
            ["match_rule_sets.organization_id", "match_rule_sets.id"],
            name="fk_match_runs_org_rule_set",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("task_run_id", name="uq_match_runs_task_run_id"),
        # Required so MatchGroup/MatchDecision/MatchSkippedBlock can each
        # have a tenant-aware composite FK (organization_id, match_run_id)
        # -> (organization_id, id) -- the Module 6 lesson, applied from
        # the start every module since.
        UniqueConstraint("organization_id", "id", name="uq_match_runs_org_id"),
        CheckConstraint(
            "status IN (" + ", ".join(f"'{s}'" for s in MATCH_RUN_STATUSES) + ")",
            name="ck_match_runs_status_valid",
        ),
        CheckConstraint("row_count >= 0", name="ck_match_runs_row_count_nonnegative"),
        CheckConstraint(
            "total_comparisons_count >= 0", name="ck_match_runs_total_comparisons_nonnegative"
        ),
        CheckConstraint(
            "duplicate_group_count >= 0", name="ck_match_runs_duplicate_group_count_nonnegative"
        ),
        CheckConstraint(
            "duplicate_pairs_count >= 0", name="ck_match_runs_duplicate_pairs_nonnegative"
        ),
        CheckConstraint(
            "ambiguous_pairs_count >= 0", name="ck_match_runs_ambiguous_pairs_nonnegative"
        ),
        CheckConstraint(
            "skipped_block_count >= 0", name="ck_match_runs_skipped_block_count_nonnegative"
        ),
        CheckConstraint(
            "confidence_score >= 0 AND confidence_score <= 1",
            name="ck_match_runs_confidence_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    task_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    data_source_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    source_task_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    rule_set_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)
    # Denormalized copy of MatchRuleSet.version at run time, so this run's
    # provenance stays fixed even if the organization later creates a
    # newer rule-set version -- same precedent StandardizationChange.
    # rule_version already established.
    rule_set_version: Mapped[int | None] = mapped_column(Integer(), nullable=True)

    row_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    # True number of pairwise comparisons actually performed, post-
    # blocking -- never the theoretical n^2 (design doc Section 3/11).
    total_comparisons_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    duplicate_group_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    # True totals, accurate even when persisted MatchDecision rows are
    # capped -- same bounded-but-never-silent contract as CleaningRun.
    # total_changes_count/StandardizationRun.total_changes_count.
    duplicate_pairs_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    ambiguous_pairs_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    # Blocks skipped for exceeding MATCH_MAX_BLOCK_SIZE -- surfaced, never
    # silent; the row-level detail behind this count lives in
    # MatchSkippedBlock (new in the approved design revision).
    skipped_block_count: Mapped[int] = mapped_column(Integer(), nullable=False)
    decisions_by_rule: Mapped[dict] = mapped_column(JSON, nullable=False)
    # Minimum confidence across every duplicate-decision that fed a group,
    # 1.0 if zero groups -- identical aggregation semantics to Module 6/7.
    confidence_score: Mapped[float] = mapped_column(Float(), nullable=False)

    match_engine_version: Mapped[str] = mapped_column(String(20), nullable=False)

    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending_review")
    approved_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rejected_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    rolled_back_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    rolled_back_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    groups: Mapped[list["MatchGroup"]] = relationship(  # noqa: F821
        back_populates="match_run", order_by="MatchGroup.canonical_row_index"
    )
    decisions: Mapped[list["MatchDecision"]] = relationship(  # noqa: F821
        back_populates="match_run"
    )
    skipped_blocks: Mapped[list["MatchSkippedBlock"]] = relationship(  # noqa: F821
        back_populates="match_run"
    )

    def __repr__(self) -> str:
        return f"MatchRun(id={self.id!r}, task_run={self.task_run_id!r}, status={self.status!r})"
