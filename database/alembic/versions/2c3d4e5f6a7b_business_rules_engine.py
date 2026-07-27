"""business rules engine

Revision ID: 2c3d4e5f6a7b
Revises: 1b2c3d4e5f6a
Create Date: 2026-07-26 15:00:00.000000

Module 21 — Reusable Business Rules Engine.

Adds 4 tables:
  1. business_rule_global_defaults  — system-seeded defaults per rule key
  2. business_rule_sets             — versioned org rule set definitions
  3. business_rule_set_items        — individual rule overrides in a set
  4. business_rule_set_runs         — audit trail: one row per pipeline run

Also seeds business_rule_global_defaults with one row per rule key defined
in the registry (app.rules.registry).

Purely additive — no existing table, column, constraint, index, or enum
value is modified.
"""
import json
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "2c3d4e5f6a7b"
down_revision: Union[str, None] = "1b2c3d4e5f6a"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# ---------------------------------------------------------------------------
# Rule registry seed data (mirrors app.rules.registry.RULE_REGISTRY_WITH_TYPES)
# Defined inline so the migration is self-contained and importable without
# the full app installed.
# ---------------------------------------------------------------------------
_RULE_SEED_ROWS = [
    # Detection rules
    {"rule_key": "detection.outlier_zscore_threshold", "rule_type": "threshold", "rule_value": 3.5, "description": "Z-score threshold above which a numeric value is flagged as an outlier."},
    {"rule_key": "detection.missing_value_severity", "rule_type": "preference", "rule_value": "MEDIUM", "description": "Severity level assigned to missing-value issues."},
    {"rule_key": "detection.required_columns", "rule_type": "list", "rule_value": [], "description": "Column names that must be present in the dataset."},
    {"rule_key": "detection.max_duplicate_rate", "rule_type": "threshold", "rule_value": 0.05, "description": "Maximum fraction of duplicate rows before the run is flagged."},
    # Remediation rules
    {"rule_key": "remediation.default_capitalization_target", "rule_type": "preference", "rule_value": "none", "description": "Default capitalization style applied when no column-level rule exists."},
    {"rule_key": "remediation.date_normalization_format", "rule_type": "preference", "rule_value": "YYYY-MM-DD", "description": "Target date format string for date normalization."},
    {"rule_key": "remediation.phone_default_country", "rule_type": "preference", "rule_value": "US", "description": "Default country code for phone normalization (ISO 3166-1 alpha-2)."},
    {"rule_key": "remediation.auto_approve_whitespace_fixes", "rule_type": "toggle", "rule_value": False, "description": "If true, whitespace-only fixes are auto-approved without human review."},
    # Validation rules
    {"rule_key": "validation.max_failure_rate", "rule_type": "threshold", "rule_value": 0.10, "description": "Maximum fraction of validation failures before the run is flagged."},
    {"rule_key": "validation.required_pass_rate", "rule_type": "threshold", "rule_value": 0.80, "description": "Minimum fraction of validations that must pass."},
    # Quality rules
    {"rule_key": "quality.pass_score_threshold", "rule_type": "threshold", "rule_value": 0.85, "description": "Overall score (0-1) at or above which the dataset receives a PASS recommendation."},
    {"rule_key": "quality.fail_score_threshold", "rule_type": "threshold", "rule_value": 0.60, "description": "Overall score (0-1) below which the dataset receives a FAIL recommendation."},
    {"rule_key": "quality.max_critical_unresolved", "rule_type": "threshold", "rule_value": 0, "description": "Maximum number of unresolved CRITICAL issues allowed for a PASS."},
    {"rule_key": "quality.max_high_unresolved", "rule_type": "threshold", "rule_value": 3, "description": "Maximum number of unresolved HIGH issues allowed for a PASS."},
    {"rule_key": "quality.auto_approve_threshold", "rule_type": "threshold", "rule_value": 0.95, "description": "Score at or above which the dataset is auto-approved without manual review."},
    {"rule_key": "quality.require_manual_review_below", "rule_type": "threshold", "rule_value": 0.70, "description": "Score below which manual review is required before export."},
    # Export rules
    {"rule_key": "export.blocked_columns", "rule_type": "list", "rule_value": [], "description": "Column names that must be excluded from clean exports."},
    {"rule_key": "export.min_quality_score", "rule_type": "threshold", "rule_value": 0.0, "description": "Minimum overall quality score (0-1) required for export."},
    {"rule_key": "export.require_zero_critical", "rule_type": "toggle", "rule_value": False, "description": "If true, export is blocked when any CRITICAL issue remains unresolved."},
    # Business validation rules
    {"rule_key": "business.required_column_names", "rule_type": "list", "rule_value": [], "description": "Column names that must be present in the dataset for business rule compliance."},
    {"rule_key": "business.forbidden_column_values", "rule_type": "severity_override", "rule_value": {}, "description": "Map of column_name -> list of forbidden values for business rule compliance."},
    {"rule_key": "business.min_row_count", "rule_type": "threshold", "rule_value": 0, "description": "Minimum number of rows required for business rule compliance."},
    # Report rules
    {"rule_key": "report.include_raw_counts", "rule_type": "toggle", "rule_value": True, "description": "If true, raw issue/change counts are included in pipeline reports."},
    {"rule_key": "report.include_lineage", "rule_type": "toggle", "rule_value": True, "description": "If true, full audit lineage is included in pipeline reports."},
]

_BUSINESS_RULE_TYPES = (
    "threshold", "toggle", "preference", "list", "severity_override",
)
_BUSINESS_RULE_SOURCES = ("builtin", "organization", "future_ai")
_BUSINESS_RULE_PIPELINE_RUN_TYPES = (
    "issue_detection", "remediation", "validation",
    "quality_control", "clean_export", "report",
)


def upgrade() -> None:
    # =========================================================================
    # 1. business_rule_global_defaults
    # =========================================================================
    op.create_table(
        "business_rule_global_defaults",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("rule_key", sa.String(255), nullable=False),
        sa.Column("rule_type", sa.String(50), nullable=False),
        sa.Column("rule_source", sa.String(20), nullable=False, server_default="builtin"),
        sa.Column("rule_value", sa.JSON(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("introduced_version", sa.String(20), nullable=False, server_default="21.0"),
        sa.Column("deprecated", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_key", name="uq_business_rule_global_defaults_rule_key"),
        sa.CheckConstraint(
            "rule_type IN (" + ", ".join(f"'{v}'" for v in _BUSINESS_RULE_TYPES) + ")",
            name="ck_business_rule_global_defaults_rule_type",
        ),
        sa.CheckConstraint(
            "rule_source IN (" + ", ".join(f"'{v}'" for v in _BUSINESS_RULE_SOURCES) + ")",
            name="ck_business_rule_global_defaults_rule_source",
        ),
    )

    # =========================================================================
    # 2. business_rule_sets
    # =========================================================================
    op.create_table(
        "business_rule_sets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("is_draft", sa.Boolean(), nullable=False, server_default="1"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default="0"),
        sa.Column("rule_schema_version", sa.String(20), nullable=False, server_default="1.0"),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("published_by_identity", sa.String(255), nullable=True),
        sa.Column("created_by_identity", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_business_rule_sets_org", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_business_rule_sets_data_source", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("organization_id", "id", name="uq_business_rule_sets_org_id"),
        sa.CheckConstraint("version >= 1", name="ck_business_rule_sets_version_min"),
    )
    op.create_index("ix_business_rule_sets_org", "business_rule_sets", ["organization_id"])
    op.create_index("ix_business_rule_sets_data_source", "business_rule_sets", ["data_source_id"])

    # Partial unique indexes (at most one active set per scope)
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE UNIQUE INDEX uq_business_rule_sets_active_org_wide "
            "ON business_rule_sets (organization_id) "
            "WHERE is_active = TRUE AND data_source_id IS NULL"
        )
        op.execute(
            "CREATE UNIQUE INDEX uq_business_rule_sets_active_ds_scoped "
            "ON business_rule_sets (organization_id, data_source_id) "
            "WHERE is_active = TRUE AND data_source_id IS NOT NULL"
        )
    else:
        op.execute(
            "CREATE UNIQUE INDEX uq_business_rule_sets_active_org_wide "
            "ON business_rule_sets (organization_id) "
            "WHERE is_active = 1 AND data_source_id IS NULL"
        )
        op.execute(
            "CREATE UNIQUE INDEX uq_business_rule_sets_active_ds_scoped "
            "ON business_rule_sets (organization_id, data_source_id) "
            "WHERE is_active = 1 AND data_source_id IS NOT NULL"
        )

    # =========================================================================
    # 3. business_rule_set_items
    # =========================================================================
    op.create_table(
        "business_rule_set_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("rule_set_id", sa.Uuid(), nullable=False),
        sa.Column("rule_key", sa.String(255), nullable=False),
        sa.Column("rule_type", sa.String(50), nullable=False),
        sa.Column("rule_source", sa.String(20), nullable=False, server_default="organization"),
        sa.Column("rule_value", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id", "rule_set_id"],
            ["business_rule_sets.organization_id", "business_rule_sets.id"],
            name="fk_business_rule_set_items_rule_set", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_business_rule_set_items_org", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_set_id", "rule_key", name="uq_business_rule_set_items_set_key"),
        sa.CheckConstraint(
            "rule_type IN (" + ", ".join(f"'{v}'" for v in _BUSINESS_RULE_TYPES) + ")",
            name="ck_business_rule_set_items_rule_type",
        ),
        sa.CheckConstraint(
            "rule_source IN (" + ", ".join(f"'{v}'" for v in _BUSINESS_RULE_SOURCES) + ")",
            name="ck_business_rule_set_items_rule_source",
        ),
    )
    op.create_index("ix_business_rule_set_items_org", "business_rule_set_items", ["organization_id"])
    op.create_index("ix_business_rule_set_items_rule_set", "business_rule_set_items", ["rule_set_id"])

    # =========================================================================
    # 4. business_rule_set_runs
    # =========================================================================
    op.create_table(
        "business_rule_set_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("rule_set_id", sa.Uuid(), nullable=True),
        sa.Column("rule_set_version", sa.Integer(), nullable=True),
        sa.Column("rule_schema_version", sa.String(20), nullable=False, server_default="1.0"),
        sa.Column("resolver_version", sa.String(20), nullable=False),
        sa.Column("pipeline_run_type", sa.String(50), nullable=False),
        sa.Column("pipeline_run_id", sa.Uuid(), nullable=False),
        sa.Column("resolved_rules_snapshot", sa.JSON(), nullable=False),
        sa.Column("resolved_rules_sha256", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("(CURRENT_TIMESTAMP)"), nullable=False),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_business_rule_set_runs_org", ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["rule_set_id"], ["business_rule_sets.id"],
            name="fk_business_rule_set_runs_rule_set", ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "pipeline_run_type", "pipeline_run_id",
            name="uq_business_rule_set_runs_pipeline_run"
        ),
        sa.CheckConstraint(
            "pipeline_run_type IN ("
            + ", ".join(f"'{v}'" for v in _BUSINESS_RULE_PIPELINE_RUN_TYPES)
            + ")",
            name="ck_business_rule_set_runs_pipeline_run_type",
        ),
    )
    op.create_index("ix_business_rule_set_runs_org", "business_rule_set_runs", ["organization_id"])
    op.create_index("ix_business_rule_set_runs_rule_set", "business_rule_set_runs", ["rule_set_id"])
    op.create_index("ix_business_rule_set_runs_pipeline_run_type", "business_rule_set_runs", ["pipeline_run_type"])
    op.create_index("ix_business_rule_set_runs_pipeline_run_id", "business_rule_set_runs", ["pipeline_run_id"])

    # =========================================================================
    # 5. Seed business_rule_global_defaults
    # =========================================================================
    table = sa.table(
        "business_rule_global_defaults",
        sa.column("id", sa.String),
        sa.column("rule_key", sa.String),
        sa.column("rule_type", sa.String),
        sa.column("rule_source", sa.String),
        sa.column("rule_value", sa.JSON),
        sa.column("description", sa.String),
        sa.column("introduced_version", sa.String),
        sa.column("deprecated", sa.Boolean),
    )
    op.bulk_insert(
        table,
        [
            {
                "id": str(uuid.uuid4()),
                "rule_key": row["rule_key"],
                "rule_type": row["rule_type"],
                "rule_source": "builtin",
                "rule_value": row["rule_value"],
                "description": row["description"],
                "introduced_version": "21.0",
                "deprecated": False,
            }
            for row in _RULE_SEED_ROWS
        ],
    )


def downgrade() -> None:
    op.drop_table("business_rule_set_runs")
    op.drop_table("business_rule_set_items")
    op.drop_index("uq_business_rule_sets_active_ds_scoped", table_name="business_rule_sets")
    op.drop_index("uq_business_rule_sets_active_org_wide", table_name="business_rule_sets")
    op.drop_index("ix_business_rule_sets_data_source", table_name="business_rule_sets")
    op.drop_index("ix_business_rule_sets_org", table_name="business_rule_sets")
    op.drop_table("business_rule_sets")
    op.drop_table("business_rule_global_defaults")
