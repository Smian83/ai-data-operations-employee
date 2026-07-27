"""BusinessRuleGlobalDefault: system-level default value for one rule key.

One row per rule key. These rows are seeded by the Alembic migration from
the central rule registry (app.rules.registry) and are read-only via the
API (no POST/PUT endpoints). Org-specific overrides live in
BusinessRuleSetItem rows instead.

No FK to users — created by the migration, not by an authenticated user.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    JSON,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import BUSINESS_RULE_SOURCES, BUSINESS_RULE_TYPES


class BusinessRuleGlobalDefault(Base):
    __tablename__ = "business_rule_global_defaults"
    __table_args__ = (
        UniqueConstraint("rule_key", name="uq_business_rule_global_defaults_rule_key"),
        CheckConstraint(
            "rule_type IN ("
            + ", ".join(f"'{v}'" for v in BUSINESS_RULE_TYPES)
            + ")",
            name="ck_business_rule_global_defaults_rule_type",
        ),
        CheckConstraint(
            "rule_source IN ("
            + ", ".join(f"'{v}'" for v in BUSINESS_RULE_SOURCES)
            + ")",
            name="ck_business_rule_global_defaults_rule_source",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    rule_key: Mapped[str] = mapped_column(String(255), nullable=False)
    rule_type: Mapped[str] = mapped_column(String(50), nullable=False)
    rule_source: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="builtin"
    )
    rule_value: Mapped[dict] = mapped_column(JSON, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    introduced_version: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="21.0"
    )
    deprecated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"BusinessRuleGlobalDefault(rule_key={self.rule_key!r}, "
            f"rule_type={self.rule_type!r})"
        )
