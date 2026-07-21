"""data export engine

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
Create Date: 2026-07-21 18:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # =========================================================================
    # No ALTER TYPE step in this migration -- TaskType.EXPORT already
    # exists in task_type_enum (added long before Module 7/8's additions),
    # currently mapped to NoOpHandler. This is the first module in this
    # project's history to add a real handler without touching the enum
    # at all. See docs/module-9-data-export-engine-design.md Section 7/19.
    # =========================================================================

    # =========================================================================
    # export_runs: one row per Module 9 EXPORT TaskRun. Direct structural
    # mirror of match_runs' idempotency/approval-state shape, plus
    # output_file_path/output_sha256 (StandardizationRun's pattern, since
    # Export -- unlike Match -- writes a real output file) and four
    # file-self-description columns added per architectural review.
    # =========================================================================
    op.create_table(
        "export_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("task_run_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=False),
        sa.Column("source_task_run_id", sa.Uuid(), nullable=False),
        sa.Column("match_run_id", sa.Uuid(), nullable=False),
        sa.Column("output_file_path", sa.String(length=1024), nullable=False),
        sa.Column("output_sha256", sa.String(length=64), nullable=False),
        sa.Column("source_row_count", sa.Integer(), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("excluded_row_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_groups_materialized_count", sa.Integer(), nullable=False),
        sa.Column("output_file_size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("output_column_count", sa.Integer(), nullable=False),
        sa.Column("export_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("csv_format_version", sa.Integer(), nullable=False),
        sa.Column("export_engine_version", sa.String(length=20), nullable=False),
        sa.Column(
            "status", sa.String(length=20), nullable=False, server_default="pending_review"
        ),
        sa.Column("approved_by", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejected_by", sa.Uuid(), nullable=True),
        sa.Column("rejected_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rolled_back_by", sa.Uuid(), nullable=True),
        sa.Column("rolled_back_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_export_runs"),
        sa.UniqueConstraint("task_run_id", name="uq_export_runs_task_run_id"),
        sa.UniqueConstraint("organization_id", "id", name="uq_export_runs_org_id"),
        sa.CheckConstraint(
            "status IN ('pending_review', 'approved', 'rejected', 'rolled_back')",
            name="ck_export_runs_status_valid",
        ),
        sa.CheckConstraint(
            "source_row_count >= 0", name="ck_export_runs_source_row_count_nonnegative"
        ),
        sa.CheckConstraint("row_count >= 0", name="ck_export_runs_row_count_nonnegative"),
        sa.CheckConstraint(
            "row_count <= source_row_count", name="ck_export_runs_row_count_le_source"
        ),
        sa.CheckConstraint(
            "excluded_row_count >= 0", name="ck_export_runs_excluded_row_count_nonnegative"
        ),
        sa.CheckConstraint(
            "excluded_row_count = source_row_count - row_count",
            name="ck_export_runs_excluded_row_count_consistent",
        ),
        sa.CheckConstraint(
            "duplicate_groups_materialized_count >= 0",
            name="ck_export_runs_duplicate_groups_materialized_nonnegative",
        ),
        sa.CheckConstraint(
            "output_file_size_bytes >= 0", name="ck_export_runs_output_file_size_nonnegative"
        ),
        sa.CheckConstraint(
            "output_column_count >= 1", name="ck_export_runs_output_column_count_min"
        ),
        sa.CheckConstraint(
            "csv_format_version >= 1", name="ck_export_runs_csv_format_version_min"
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_export_runs_organization_id_organizations", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_export_runs_org_task_run", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_export_runs_org_task", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_export_runs_org_data_source", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "source_task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_export_runs_org_source_task_run", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "match_run_id"],
            ["match_runs.organization_id", "match_runs.id"],
            name="fk_export_runs_org_match_run", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"], ["users.id"],
            name="fk_export_runs_approved_by_users", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["rejected_by"], ["users.id"],
            name="fk_export_runs_rejected_by_users", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["rolled_back_by"], ["users.id"],
            name="fk_export_runs_rolled_back_by_users", ondelete="SET NULL",
        ),
    )
    op.create_index("ix_export_runs_organization_id", "export_runs", ["organization_id"])
    op.create_index("ix_export_runs_task_run_id", "export_runs", ["task_run_id"], unique=True)
    op.create_index("ix_export_runs_task_id", "export_runs", ["task_id"])
    op.create_index("ix_export_runs_data_source_id", "export_runs", ["data_source_id"])
    op.create_index(
        "ix_export_runs_source_task_run_id", "export_runs", ["source_task_run_id"]
    )
    op.create_index("ix_export_runs_match_run_id", "export_runs", ["match_run_id"])

    # =========================================================================
    # export_row_exclusions: one row per record excluded from an
    # ExportRun's output file, capped per run at
    # settings.export_max_persisted_exclusions.
    # =========================================================================
    op.create_table(
        "export_row_exclusions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("export_run_id", sa.Uuid(), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("match_group_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_row_index", sa.Integer(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_export_row_exclusions"),
        sa.UniqueConstraint(
            "export_run_id", "row_index", name="uq_export_row_exclusions_run_row"
        ),
        sa.CheckConstraint(
            "row_index >= 0", name="ck_export_row_exclusions_row_index_nonnegative"
        ),
        sa.CheckConstraint(
            "canonical_row_index >= 0",
            name="ck_export_row_exclusions_canonical_row_index_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "export_run_id"],
            ["export_runs.organization_id", "export_runs.id"],
            name="fk_export_row_exclusions_org_export_run", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "match_group_id"],
            ["match_groups.organization_id", "match_groups.id"],
            name="fk_export_row_exclusions_org_match_group", ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_export_row_exclusions_organization_id", "export_row_exclusions", ["organization_id"]
    )
    op.create_index(
        "ix_export_row_exclusions_export_run_id", "export_row_exclusions", ["export_run_id"]
    )
    op.create_index(
        "ix_export_row_exclusions_match_group_id", "export_row_exclusions", ["match_group_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_export_row_exclusions_match_group_id", table_name="export_row_exclusions"
    )
    op.drop_index(
        "ix_export_row_exclusions_export_run_id", table_name="export_row_exclusions"
    )
    op.drop_index(
        "ix_export_row_exclusions_organization_id", table_name="export_row_exclusions"
    )
    op.drop_table("export_row_exclusions")

    op.drop_index("ix_export_runs_match_run_id", table_name="export_runs")
    op.drop_index("ix_export_runs_source_task_run_id", table_name="export_runs")
    op.drop_index("ix_export_runs_data_source_id", table_name="export_runs")
    op.drop_index("ix_export_runs_task_id", table_name="export_runs")
    op.drop_index("ix_export_runs_task_run_id", table_name="export_runs")
    op.drop_index("ix_export_runs_organization_id", table_name="export_runs")
    op.drop_table("export_runs")
