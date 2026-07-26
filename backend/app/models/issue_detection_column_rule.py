"""
IssueDetectionColumnRule: organization-configured, per-column governance
for every Module 14 check that must never guess a column's meaning from
its name or its data (the approved Module 14 corrections). Structural
sibling of StandardizationColumnMapping (Module 7) -- same tenant-scoped,
soft-deletable convention, scoped either to one data_source or (when
data_source_id is NULL) to every data source in that organization, and the
same two-partial-unique-index reasoning (NULL != NULL under standard SQL
uniqueness semantics, so a single plain unique index cannot correctly
express "at most one active org-wide rule per column name").

Unlike StandardizationColumnMapping (one purpose: field_type), this table
carries every column-level toggle Module 14's detectors read, because they
all share the identical governance rule: a check only runs for a column
when a row here explicitly turns it on. No column here has a "guess from
data" fallback anywhere in app.detection -- an unconfigured column simply
never triggers required_field_violation / invalid_enum_value / outlier /
inconsistent_capitalization / duplicate_primary_key / expected_type
checks, no matter what its name or contents look like. See
docs/module-14-issue-detection-engine-design.md (added when this module's
docs are written) and app.detection.rules for the read side of this
table.

expected_type drives invalid_email / invalid_phone / invalid_date /
invalid_numeric / boolean_inconsistency: NULL means "no format check for
this column" -- is_required / is_primary_key / allowed_values / the
outlier/capitalization toggles are independent of it and of each other, a
column can set any combination.

capitalization_check_enabled defaults to False -- per the approved Module
14 corrections, inconsistent-capitalization detection would produce
excessive false positives on names, IDs, product codes, and acronyms if it
ran unconditionally, so it is opt-in per column like everything else here,
not merely configurable in the abstract.
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
    JSON,
    String,
    Uuid,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import ISSUE_DETECTION_EXPECTED_TYPES


class IssueDetectionColumnRule(Base):
    __tablename__ = "issue_detection_column_rules"
    __table_args__ = (
        ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_issue_detection_column_rules_org_data_source",
            ondelete="RESTRICT",
        ),
        Index(
            "ix_issue_detection_column_rules_scoped_active",
            "organization_id",
            "data_source_id",
            text("lower(trim(column_name))"),
            unique=True,
            postgresql_where=text("is_active = true AND data_source_id IS NOT NULL"),
            sqlite_where=text("is_active = 1 AND data_source_id IS NOT NULL"),
        ),
        Index(
            "ix_issue_detection_column_rules_orgwide_active",
            "organization_id",
            text("lower(trim(column_name))"),
            unique=True,
            postgresql_where=text("is_active = true AND data_source_id IS NULL"),
            sqlite_where=text("is_active = 1 AND data_source_id IS NULL"),
        ),
        CheckConstraint(
            "expected_type IS NULL OR expected_type IN ("
            + ", ".join(f"'{t}'" for t in ISSUE_DETECTION_EXPECTED_TYPES)
            + ")",
            name="ck_issue_detection_column_rules_expected_type_valid",
        ),
        CheckConstraint(
            "outlier_zscore_threshold IS NULL OR outlier_zscore_threshold > 0",
            name="ck_issue_detection_column_rules_outlier_threshold_positive",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # NULL = applies to every data source in this organization.
    data_source_id: Mapped[uuid.UUID | None] = mapped_column(Uuid(), nullable=True)
    column_name: Mapped[str] = mapped_column(String(255), nullable=False)

    # NULL = no email/phone/date/numeric/boolean format check for this
    # column. See app.models.enums.ISSUE_DETECTION_EXPECTED_TYPES.
    expected_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    is_required: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    is_primary_key: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    # NULL/empty = no enum/category restriction for this column. A JSON
    # array of the exact allowed string values (same JSON-column precedent
    # as DataSource.connection_metadata / DataProfile.column_profiles).
    allowed_values: Mapped[list | None] = mapped_column(JSON, nullable=True)
    outlier_enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    # NULL = fall back to settings.issue_detection_outlier_zscore_threshold.
    outlier_zscore_threshold: Mapped[float | None] = mapped_column(Float(), nullable=True)
    # Default False -- see module docstring.
    capitalization_check_enabled: Mapped[bool] = mapped_column(
        Boolean(), nullable=False, default=False
    )

    is_active: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"IssueDetectionColumnRule(id={self.id!r}, org={self.organization_id!r}, "
            f"column={self.column_name!r}, expected_type={self.expected_type!r})"
        )
