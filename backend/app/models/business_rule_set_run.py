"""BusinessRuleSetRun: audit record linking one pipeline run to the rules used.

One row per (pipeline_run_type, pipeline_run_id) — enforced by UNIQUE.
Captures the full resolved-rules snapshot and its SHA-256 so the exact
rule values can be audited without re-running resolution.

rule_set_id is nullable: NULL for pipeline runs executed before M21 was
deployed, or when no org rule sets existed and only global defaults were used.
"""
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
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import BUSINESS_RULE_PIPELINE_RUN_TYPES
from app.rules.registry import RULE_SCHEMA_VERSION
from app.rules.resolver import RESOLVER_VERSION


class BusinessRuleSetRun(Base):
    __tablename__ = "business_rule_set_runs"
    __table_args__ = (
        # ---- FK → organizations ----
        ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_business_rule_set_runs_org",
            ondelete="CASCADE",
        ),
        # ---- Optional FK → business_rule_sets (nullable) ----
        ForeignKeyConstraint(
            ["rule_set_id"],
            ["business_rule_sets.id"],
            name="fk_business_rule_set_runs_rule_set",
            ondelete="SET NULL",
        ),
        # ---- Idempotency: one audit row per pipeline run ----
        UniqueConstraint(
            "pipeline_run_type", "pipeline_run_id",
            name="uq_business_rule_set_runs_pipeline_run",
        ),
        CheckConstraint(
            "pipeline_run_type IN ("
            + ", ".join(f"'{v}'" for v in BUSINESS_RULE_PIPELINE_RUN_TYPES)
            + ")",
            name="ck_business_rule_set_runs_pipeline_run_type",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rule_set_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(),
        ForeignKey("business_rule_sets.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    rule_set_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    rule_schema_version: Mapped[str] = mapped_column(
        String(20), nullable=False, default=RULE_SCHEMA_VERSION
    )
    resolver_version: Mapped[str] = mapped_column(
        String(20), nullable=False, default=RESOLVER_VERSION
    )
    pipeline_run_type: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    pipeline_run_id: Mapped[uuid.UUID] = mapped_column(Uuid(), nullable=False, index=True)
    resolved_rules_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    resolved_rules_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"BusinessRuleSetRun(pipeline_run_type={self.pipeline_run_type!r}, "
            f"pipeline_run_id={self.pipeline_run_id!r})"
        )
