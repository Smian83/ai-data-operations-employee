"""
MatchRuleSet: organization-configured, versioned matching configuration
(Section 3 of docs/module-8-data-matching-deduplication-design.md).
Immutable once created -- changing an organization's matching
configuration means creating a new, higher-version row and deactivating
the old one (is_active=false), never editing rows in place. Same
tenant-scoped, soft-deletable, partial-unique-index-on-the-active-scope
convention StandardizationColumnMapping already established for a
nullable-scope column (data_source_id here).
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class MatchRuleSet(Base):
    __tablename__ = "match_rule_sets"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_match_rule_sets_org_data_source",
            ondelete="RESTRICT",
        ),
        # Required so MatchRun.rule_set_id and MatchRuleField.rule_set_id
        # can each have a tenant-aware composite FK -> (organization_id,
        # id), same pattern as every other Module 6/7/8 parent table.
        UniqueConstraint("organization_id", "id", name="uq_match_rule_sets_org_id"),
        CheckConstraint("version >= 1", name="ck_match_rule_sets_version_min"),
        CheckConstraint(
            "review_threshold >= 0 AND review_threshold < duplicate_threshold "
            "AND duplicate_threshold <= 1",
            name="ck_match_rule_sets_threshold_order",
        ),
        Index(
            "ix_match_rule_sets_scoped_active",
            "organization_id",
            "data_source_id",
            unique=True,
            postgresql_where=text("is_active = true AND data_source_id IS NOT NULL"),
            sqlite_where=text("is_active = 1 AND data_source_id IS NOT NULL"),
        ),
        Index(
            "ix_match_rule_sets_orgwide_active",
            "organization_id",
            unique=True,
            postgresql_where=text("is_active = true AND data_source_id IS NULL"),
            sqlite_where=text("is_active = 1 AND data_source_id IS NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # NULL = applies to every data source in this organization.
    data_source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    # Server-computed at creation time as "one more than this
    # organization's current count for this scope" -- never client-
    # supplied.
    version: Mapped[int] = mapped_column(Integer(), nullable=False)
    duplicate_threshold: Mapped[float] = mapped_column(Float(), nullable=False)
    review_threshold: Mapped[float] = mapped_column(Float(), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    fields: Mapped[list["MatchRuleField"]] = relationship(  # noqa: F821
        back_populates="rule_set", order_by="MatchRuleField.created_at"
    )

    def __repr__(self) -> str:
        return (
            f"MatchRuleSet(id={self.id!r}, org={self.organization_id!r}, "
            f"version={self.version!r}, is_active={self.is_active!r})"
        )
