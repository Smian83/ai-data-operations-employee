"""issue detection engine

Revision ID: f7a8b9c0d1e2
Revises: e6f7a8b9c0d1
Create Date: 2026-07-24 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f7a8b9c0d1e2"
down_revision: Union[str, None] = "e6f7a8b9c0d1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # =========================================================================
    # task_type_enum: add 'detect' to the existing native PostgreSQL enum
    # type. Third ALTER TYPE ... ADD VALUE in this project (after
    # d4e5f6a7b8c9's 'standardize' and f1a2b3c4d5e6's 'match') -- same
    # restriction applies: the new label cannot be used within the same
    # transaction it is added in, so this runs in its own autocommit block.
    #
    # SQLite has no native enum type and needs no equivalent change here --
    # same reasoning as every prior enum-extension migration in this
    # project: tasks.task_type there is a plain VARCHAR with no CHECK
    # constraint from these migration-local enum objects (only the ORM
    # model's own copy sets create_constraint=True, and Alembic migrations
    # never consult the ORM model).
    # =========================================================================
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE task_type_enum ADD VALUE IF NOT EXISTS 'detect'")

    # =========================================================================
    # issue_detection_runs: one row per Module 14 DETECT TaskRun. Structural
    # sibling of data_profiles -- no status/approval columns, since the
    # engine never mutates anything for a human to approve.
    # =========================================================================
    op.create_table(
        "issue_detection_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("task_run_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=False),
        sa.Column("rows_scanned", sa.Integer(), nullable=False),
        sa.Column("columns_scanned", sa.Integer(), nullable=False),
        sa.Column("total_issues_found", sa.Integer(), nullable=False),
        sa.Column("persisted_issue_count", sa.Integer(), nullable=False),
        sa.Column("issues_by_severity", sa.JSON(), nullable=False),
        sa.Column("issues_by_type", sa.JSON(), nullable=False),
        sa.Column("limits_applied", sa.JSON(), nullable=False),
        sa.Column("detection_engine_version", sa.String(length=20), nullable=False),
        sa.Column(
            "detected_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_issue_detection_runs"),
        sa.UniqueConstraint("task_run_id", name="uq_issue_detection_runs_task_run_id"),
        sa.UniqueConstraint("organization_id", "id", name="uq_issue_detection_runs_org_id"),
        sa.CheckConstraint(
            "rows_scanned >= 0", name="ck_issue_detection_runs_rows_scanned_nonneg"
        ),
        sa.CheckConstraint(
            "columns_scanned >= 0", name="ck_issue_detection_runs_columns_scanned_nonneg"
        ),
        sa.CheckConstraint(
            "total_issues_found >= 0", name="ck_issue_detection_runs_total_issues_nonneg"
        ),
        sa.CheckConstraint(
            "persisted_issue_count >= 0 AND persisted_issue_count <= total_issues_found",
            name="ck_issue_detection_runs_persisted_count_valid",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_issue_detection_runs_organization_id_organizations", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_issue_detection_runs_org_task_run", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_issue_detection_runs_org_task", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_issue_detection_runs_org_data_source", ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_issue_detection_runs_organization_id", "issue_detection_runs", ["organization_id"]
    )
    op.create_index(
        "ix_issue_detection_runs_task_run_id", "issue_detection_runs", ["task_run_id"],
        unique=True,
    )
    op.create_index("ix_issue_detection_runs_task_id", "issue_detection_runs", ["task_id"])
    op.create_index(
        "ix_issue_detection_runs_data_source_id", "issue_detection_runs", ["data_source_id"]
    )

    # =========================================================================
    # issues: append-only per-finding record, capped per run at
    # settings.issue_detection_max_persisted_issues. issue_type/severity are
    # plain, CHECK-validated strings -- same "small, internal, worker/
    # config-owned value set -> plain string, not a native Postgres enum"
    # precedent every closed vocabulary in this project uses (see
    # app.models.enums's own module-level comments).
    # =========================================================================
    issue_types_sql = (
        "'missing_value', 'empty_string', 'null_value', 'duplicate_row', "
        "'duplicate_primary_key', 'invalid_email', 'invalid_phone', 'invalid_date', "
        "'invalid_numeric', 'required_field_violation', 'leading_whitespace', "
        "'trailing_whitespace', 'multiple_internal_spaces', 'inconsistent_capitalization', "
        "'boolean_inconsistency', 'invalid_enum_value', 'outlier', 'broken_fk_reference'"
    )
    severities_sql = "'INFO', 'LOW', 'MEDIUM', 'HIGH', 'CRITICAL'"
    op.create_table(
        "issues",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("detection_run_id", sa.Uuid(), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("column_name", sa.String(length=255), nullable=True),
        sa.Column("issue_type", sa.String(length=30), nullable=False),
        sa.Column("severity", sa.String(length=10), nullable=False),
        sa.Column("original_value", sa.Text(), nullable=True),
        sa.Column("suggested_fix", sa.Text(), nullable=True),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_issues"),
        sa.CheckConstraint(f"issue_type IN ({issue_types_sql})", name="ck_issues_issue_type_valid"),
        sa.CheckConstraint(f"severity IN ({severities_sql})", name="ck_issues_severity_valid"),
        sa.CheckConstraint("row_number >= 0", name="ck_issues_row_number_nonnegative"),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1", name="ck_issues_confidence_range"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_issues_organization_id_organizations", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "detection_run_id"],
            ["issue_detection_runs.organization_id", "issue_detection_runs.id"],
            name="fk_issues_org_detection_run", ondelete="CASCADE",
        ),
    )
    op.create_index("ix_issues_organization_id", "issues", ["organization_id"])
    op.create_index("ix_issues_detection_run_id", "issues", ["detection_run_id"])
    op.create_index("ix_issues_issue_type", "issues", ["issue_type"])
    op.create_index("ix_issues_severity", "issues", ["severity"])

    # =========================================================================
    # issue_detection_column_rules: organization-configured, per-column
    # governance for every check that must not guess a column's meaning
    # from its name or data (approved Module 14 corrections). Same
    # two-partial-unique-index reasoning as standardization_column_mappings
    # -- data_source_id is nullable ("applies org-wide") and NULL != NULL
    # under standard SQL uniqueness semantics.
    # =========================================================================
    expected_types_sql = "'email', 'phone', 'date', 'numeric', 'boolean'"
    op.create_table(
        "issue_detection_column_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=True),
        sa.Column("column_name", sa.String(length=255), nullable=False),
        sa.Column("expected_type", sa.String(length=20), nullable=True),
        sa.Column("is_required", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("is_primary_key", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("allowed_values", sa.JSON(), nullable=True),
        sa.Column("outlier_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("outlier_zscore_threshold", sa.Float(), nullable=True),
        sa.Column(
            "capitalization_check_enabled", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_issue_detection_column_rules"),
        sa.CheckConstraint(
            f"expected_type IS NULL OR expected_type IN ({expected_types_sql})",
            name="ck_issue_detection_column_rules_expected_type_valid",
        ),
        sa.CheckConstraint(
            "outlier_zscore_threshold IS NULL OR outlier_zscore_threshold > 0",
            name="ck_issue_detection_column_rules_outlier_threshold_positive",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_issue_detection_column_rules_org_id",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_issue_detection_column_rules_org_data_source", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"],
            name="fk_issue_detection_column_rules_created_by_users", ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_issue_detection_column_rules_organization_id",
        "issue_detection_column_rules", ["organization_id"],
    )
    op.create_index(
        "ix_issue_detection_column_rules_scoped_active",
        "issue_detection_column_rules",
        ["organization_id", "data_source_id", sa.text("lower(trim(column_name))")],
        unique=True,
        postgresql_where=sa.text("is_active = true AND data_source_id IS NOT NULL"),
        sqlite_where=sa.text("is_active = 1 AND data_source_id IS NOT NULL"),
    )
    op.create_index(
        "ix_issue_detection_column_rules_orgwide_active",
        "issue_detection_column_rules",
        ["organization_id", sa.text("lower(trim(column_name))")],
        unique=True,
        postgresql_where=sa.text("is_active = true AND data_source_id IS NULL"),
        sqlite_where=sa.text("is_active = 1 AND data_source_id IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_issue_detection_column_rules_orgwide_active",
        table_name="issue_detection_column_rules",
    )
    op.drop_index(
        "ix_issue_detection_column_rules_scoped_active",
        table_name="issue_detection_column_rules",
    )
    op.drop_index(
        "ix_issue_detection_column_rules_organization_id",
        table_name="issue_detection_column_rules",
    )
    op.drop_table("issue_detection_column_rules")

    op.drop_index("ix_issues_severity", table_name="issues")
    op.drop_index("ix_issues_issue_type", table_name="issues")
    op.drop_index("ix_issues_detection_run_id", table_name="issues")
    op.drop_index("ix_issues_organization_id", table_name="issues")
    op.drop_table("issues")

    op.drop_index("ix_issue_detection_runs_data_source_id", table_name="issue_detection_runs")
    op.drop_index("ix_issue_detection_runs_task_id", table_name="issue_detection_runs")
    op.drop_index("ix_issue_detection_runs_task_run_id", table_name="issue_detection_runs")
    op.drop_index("ix_issue_detection_runs_organization_id", table_name="issue_detection_runs")
    op.drop_table("issue_detection_runs")

    # task_type_enum: PostgreSQL has no DROP VALUE for enum types. Same
    # documented, accepted limitation as d4e5f6a7b8c9's 'standardize' and
    # f1a2b3c4d5e6's 'match' -- removing 'detect' would require rebuilding
    # the entire type and every column/table that references it, out of
    # proportion to what a downgrade needs to accomplish. This does not
    # affect downgrade->upgrade cycle correctness: no row will contain
    # 'detect' after a genuine downgrade in a fresh verification database,
    # since issue_detection_runs (the only table that would ever reference
    # it indirectly) is dropped above, and tasks.task_type is a plain
    # string column, not itself checked against this specific value by any
    # CHECK constraint added in this migration.
