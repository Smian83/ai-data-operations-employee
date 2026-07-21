"""
MatchRuleField: the weighted field list for one MatchRuleSet (Section 3
of docs/module-8-data-matching-deduplication-design.md). Created
together with its parent MatchRuleSet row in a single API call and never
edited or added to afterward -- a rule set's field list is part of its
immutable, versioned definition.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Float,
    ForeignKeyConstraint,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import MATCH_RULE_COMPARISON_TYPES


class MatchRuleField(Base):
    __tablename__ = "match_rule_fields"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "rule_set_id"],
            ["match_rule_sets.organization_id", "match_rule_sets.id"],
            name="fk_match_rule_fields_org_rule_set",
            ondelete="CASCADE",
        ),
        # The same column can't be configured twice within one rule set
        # (would make weight-summing ambiguous).
        UniqueConstraint("rule_set_id", "column_name", name="uq_match_rule_fields_set_column"),
        CheckConstraint(
            "comparison_type IN ("
            + ", ".join(f"'{t}'" for t in MATCH_RULE_COMPARISON_TYPES)
            + ")",
            name="ck_match_rule_fields_comparison_type_valid",
        ),
        CheckConstraint("weight > 0", name="ck_match_rule_fields_weight_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    rule_set_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)

    # Matched case-insensitively against the standardized CSV's headers,
    # same convention StandardizationColumnMapping.column_name uses.
    column_name: Mapped[str] = mapped_column(String(255), nullable=False)
    comparison_type: Mapped[str] = mapped_column(String(20), nullable=False)
    weight: Mapped[float] = mapped_column(Float(), nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    rule_set: Mapped["MatchRuleSet"] = relationship(back_populates="fields")  # noqa: F821

    def __repr__(self) -> str:
        return (
            f"MatchRuleField(id={self.id!r}, rule_set={self.rule_set_id!r}, "
            f"column={self.column_name!r}, weight={self.weight!r})"
        )
