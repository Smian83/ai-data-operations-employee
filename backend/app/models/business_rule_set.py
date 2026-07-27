"""BusinessRuleSet: a versioned collection of org-specific rule overrides.

Scoping:
  data_source_id IS NULL  → org-wide set (applies to all data sources)
  data_source_id IS NOT NULL → data-source-specific set (highest precedence)

Lifecycle:
  is_draft=True, is_active=False  → draft (items can be added/edited/removed)
  is_draft=False, is_active=False → published but not yet active
  is_draft=False, is_active=True  → active (used by resolver)

At most one active set per scope (org_id + data_source_id) is enforced by
a partial unique index.

version is server-computed on publish. The first publish sets version=1.
Subsequent publishes of new drafts for the same scope increment the version.
"""
import uuid
from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.rules.registry import RULE_SCHEMA_VERSION


class BusinessRuleSet(Base):
    __tablename__ = "business_rule_sets"
    __table_args__ = (
        # ---- Composite FK → organizations ----
        ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_business_rule_sets_org",
            ondelete="CASCADE",
        ),
        # ---- Composite FK → data_sources (when data_source_id IS NOT NULL) ----
        ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_business_rule_sets_data_source",
            ondelete="RESTRICT",
        ),
        # ---- Required for composite FK target from business_rule_set_items ----
        UniqueConstraint(
            "organization_id", "id", name="uq_business_rule_sets_org_id"
        ),
        # ---- version >= 1 ----
        CheckConstraint("version >= 1", name="ck_business_rule_sets_version_min"),
        # ---- Partial unique index: at most one active set per scope ----
        # (org-wide: data_source_id IS NULL)
        Index(
            "uq_business_rule_sets_active_org_wide",
            "organization_id",
            unique=True,
            postgresql_where=text("is_active = TRUE AND data_source_id IS NULL"),
            sqlite_where=text("is_active = 1 AND data_source_id IS NULL"),
        ),
        # (DS-scoped: data_source_id IS NOT NULL)
        Index(
            "uq_business_rule_sets_active_ds_scoped",
            "organization_id",
            "data_source_id",
            unique=True,
            postgresql_where=text(
                "is_active = TRUE AND data_source_id IS NOT NULL"
            ),
            sqlite_where=text(
                "is_active = 1 AND data_source_id IS NOT NULL"
            ),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    data_source_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), nullable=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_draft: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    rule_schema_version: Mapped[str] = mapped_column(
        String(20), nullable=False, default=RULE_SCHEMA_VERSION
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    published_by_identity: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    created_by_identity: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    items: Mapped[list["BusinessRuleSetItem"]] = relationship(  # noqa: F821
        back_populates="rule_set",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )

    def __repr__(self) -> str:
        return (
            f"BusinessRuleSet(id={self.id!r}, name={self.name!r}, "
            f"is_active={self.is_active!r}, version={self.version!r})"
        )
