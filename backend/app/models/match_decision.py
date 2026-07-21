"""
MatchDecision: one row per pairwise comparison that reached at least the
review threshold (Section 3 of
docs/module-8-data-matching-deduplication-design.md). The granular,
potentially large audit table -- capped per run at
MATCH_MAX_PERSISTED_DECISIONS, same bounded pattern as CleaningChange/
StandardizationChange. Append-only, never updated after creation.

blocking_key (approved design revision): the normalized blocking-key
value shared by both compared rows for Stage-2 decisions -- the direct,
inspectable reason the pair was ever compared at all. NULL for Stage-1
(exact_row_match) decisions, which use no blocking (full-row key
instead). See design doc Section 3/6/9.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKeyConstraint,
    Index,
    Integer,
    JSON,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import MATCH_DECISION_TYPES


class MatchDecision(Base):
    __tablename__ = "match_decisions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "match_run_id"],
            ["match_runs.organization_id", "match_runs.id"],
            name="fk_match_decisions_org_match_run",
            ondelete="CASCADE",
        ),
        # Nullable -- NULL for 'ambiguous' decisions, which are never
        # grouped (structural enforcement of "ambiguous matches are never
        # silently merged", not just a policy note; see design Section 6).
        ForeignKeyConstraint(
            ["organization_id", "match_group_id"],
            ["match_groups.organization_id", "match_groups.id"],
            name="fk_match_decisions_org_match_group",
            ondelete="CASCADE",
        ),
        Index(
            "ix_match_decisions_run_blocking_key", "match_run_id", "blocking_key"
        ),
        CheckConstraint(
            "record_a_row_index < record_b_row_index",
            name="ck_match_decisions_row_order",
        ),
        CheckConstraint(
            "total_score >= 0 AND total_score <= 1", name="ck_match_decisions_total_score_range"
        ),
        CheckConstraint(
            "threshold_used >= 0 AND threshold_used <= 1",
            name="ck_match_decisions_threshold_range",
        ),
        CheckConstraint(
            "decision IN (" + ", ".join(f"'{d}'" for d in MATCH_DECISION_TYPES) + ")",
            name="ck_match_decisions_decision_valid",
        ),
        CheckConstraint(
            "confidence_score >= 0 AND confidence_score <= 1",
            name="ck_match_decisions_confidence_range",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    match_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    match_group_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True, index=True)

    record_a_row_index: Mapped[int] = mapped_column(Integer(), nullable=False)
    record_b_row_index: Mapped[int] = mapped_column(Integer(), nullable=False)
    # Normalized blocking-key value shared by both rows (Stage 2), or NULL
    # for Stage-1 (exact_row_match) decisions, which use no blocking.
    blocking_key: Mapped[str | None] = mapped_column(String(500), nullable=True)
    rule_name: Mapped[str] = mapped_column(String(50), nullable=False)
    # One JSON column consolidating compared fields, normalized compared
    # values, and field-level scores (Section 3): {"<column>": {
    # "value_a", "value_b", "matched", "weight", "contribution"}, ...}.
    field_comparisons: Mapped[dict] = mapped_column(JSON, nullable=False)
    total_score: Mapped[float] = mapped_column(Float(), nullable=False)
    threshold_used: Mapped[float] = mapped_column(Float(), nullable=False)
    decision: Mapped[str] = mapped_column(String(20), nullable=False)
    # Equal to total_score in this initial deterministic design (Section
    # 7) -- Stage 1 (exact_row_match) is the one fixed case (1.0).
    confidence_score: Mapped[float] = mapped_column(Float(), nullable=False)
    reason: Mapped[str] = mapped_column(Text(), nullable=False)
    # MATCH_ENGINE_VERSION in effect when THIS row was produced -- the
    # code engine version, not the org's rule-set version (that is
    # separately available via the parent MatchRun.rule_set_version).
    rule_version: Mapped[str] = mapped_column(String(20), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    match_run: Mapped["MatchRun"] = relationship(  # noqa: F821
        back_populates="decisions", overlaps="decisions"
    )
    match_group: Mapped["MatchGroup | None"] = relationship(  # noqa: F821
        back_populates="decisions", overlaps="decisions,match_run"
    )

    def __repr__(self) -> str:
        return (
            f"MatchDecision(id={self.id!r}, match_run={self.match_run_id!r}, "
            f"pair=({self.record_a_row_index!r}, {self.record_b_row_index!r}), "
            f"decision={self.decision!r})"
        )
