"""
RemediationColumnRule: organization-configured, per-column governance for
every Module 15 remediation action that must never guess (the approved
Module 15 decisions). Structural sibling of IssueDetectionColumnRule
(Module 14) and StandardizationColumnMapping (Module 7) -- same
tenant-scoped, soft-deletable convention, scoped either to one
data_source or (when data_source_id is NULL) to every data source in that
organization, and the same two-partial-unique-index pattern (NULL != NULL
under standard SQL uniqueness semantics).

Holds only the configuration Module 14's own IssueDetectionColumnRule does
not already capture: invalid_enum_value remediation reuses
IssueDetectionColumnRule.allowed_values directly, read-only -- it is not
duplicated here. See docs/module-15-deterministic-cleaning-engine-design.md
Section 3.

Every field here is a hard gate, not a hint: no standardize_capitalization
proposal is ever generated for a column without an explicit
capitalization_target (Module 14's own "dominant pattern" is not treated
as correct); no normalize_date proposal without an explicit
source_date_format (never a multi-format guess -- see the design doc's
Risk R1); no normalize_phone proposal without an explicit default_country
(same "never guess a country" reasoning Module 7 already established for
its own phone standardization).
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
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base

CAPITALIZATION_TARGETS = ("lower", "upper", "title")


class RemediationColumnRule(Base):
    __tablename__ = "remediation_column_rules"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_remediation_column_rules_org_data_source",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_remediation_column_rules_scoped_active",
            "organization_id",
            "data_source_id",
            text("lower(trim(column_name))"),
            unique=True,
            postgresql_where=text("is_active = true AND data_source_id IS NOT NULL"),
            sqlite_where=text("is_active = 1 AND data_source_id IS NOT NULL"),
        ),
        Index(
            "ix_remediation_column_rules_orgwide_active",
            "organization_id",
            text("lower(trim(column_name))"),
            unique=True,
            postgresql_where=text("is_active = true AND data_source_id IS NULL"),
            sqlite_where=text("is_active = 1 AND data_source_id IS NULL"),
        ),
        CheckConstraint(
            "capitalization_target IS NULL OR capitalization_target IN ("
            + ", ".join(f"'{t}'" for t in CAPITALIZATION_TARGETS)
            + ")",
            name="ck_remediation_column_rules_capitalization_target_valid",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # NULL = applies to every data source in this organization.
    data_source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    column_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # NULL = no date remediation for this column -- a strptime format
    # string (e.g. "%m/%d/%Y"), applied as a single deterministic parse,
    # never a multi-format guess.
    source_date_format: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # NULL = no date remediation for this column -- a strftime format
    # string the parsed value is rendered back into (e.g. "%Y-%m-%d" for a
    # date-only target, or "%Y-%m-%dT%H:%M:%S" for a datetime target).
    # Required alongside source_date_format: normalize_date never proposes
    # a change without both explicitly configured, and never infers a
    # target shape from the source format -- see Module 15 decision 10
    # ("never guess dates"), extended here to the output side as well.
    target_date_format: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # NULL = no phone remediation for this column -- ISO 3166-1 alpha-2.
    default_country: Mapped[str | None] = mapped_column(String(2), nullable=True)
    # NULL = no capitalization remediation for this column.
    capitalization_target: Mapped[str | None] = mapped_column(String(10), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"RemediationColumnRule(id={self.id!r}, org={self.organization_id!r}, "
            f"column={self.column_name!r})"
        )
