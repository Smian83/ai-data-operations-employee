"""BusinessRuleSetItem: one rule override within a BusinessRuleSet.

At most one item per (rule_set_id, rule_key) — enforced by UNIQUE constraint.
Items can only be added, updated, or removed while the rule set is a draft.

The composite FK to business_rule_sets uses (organization_id, rule_set_id)
referencing (organization_id, id) — the same composite FK pattern the rest of
the project uses for cross-table org isolation.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    JSON,
    String,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import BUSINESS_RULE_SOURCES, BUSINESS_RULE_TYPES


class BusinessRuleSetItem(Base):
    __tablename__ = "business_rule_set_items"
    __table_args__ = (
        # ---- Composite FK → business_rule_sets ----
        ForeignKeyConstraint(
            ["organization_id", "rule_set_id"],
            ["business_rule_sets.organization_id", "business_rule_sets.id"],
            name="fk_business_rule_set_items_rule_set",
            ondelete="CASCADE",
        ),
        # ---- FK → organizations ----
        ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_business_rule_set_items_org",
            ondelete="CASCADE",
        ),
        # ---- One item per rule_key per rule set ----
        UniqueConstraint(
            "rule_set_id", "rule_key",
            name="uq_business_rule_set_items_set_key",
        ),
        CheckConstraint(
            "rule_type IN ("
            + ", ".join(f"'{v}'" for v in BUSINESS_RULE_TYPES)
            + ")",
            name="ck_business_rule_set_items_rule_type",
        ),
        CheckConstraint(
            "rule_source IN ("
            + ", ".join(f"'{v}'" for v in BUSINESS_RULE_SOURCES)
            + ")",
            name="ck_business_rule_set_items_rule_source",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rule_set_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    rule_key: Mapped[str] = mapped_column(String(255), nullable=False)
    rule_type: Mapped[str] = mapped_column(String(50), nullable=False)
    rule_source: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="organization"
    )
    rule_value: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    rule_set: Mapped["BusinessRuleSet"] = relationship(  # noqa: F821
        back_populates="items",
    )

    def __repr__(self) -> str:
        return (
            f"BusinessRuleSetItem(rule_set_id={self.rule_set_id!r}, "
            f"rule_key={self.rule_key!r})"
        )
