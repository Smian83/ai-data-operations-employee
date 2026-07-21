"""data cleaning engine

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-07-20 22:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f6a7b8"
down_revision: Union[str, None] = "b2c3d4e5f6a7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # =========================================================================
    # task_runs: one additive, nullable, self-referential column. Only
    # meaningful for TRANSFORM runs (identifies which prior SYNC run's
    # DataProfile is being cleaned); NULL for every existing row and every
    # other task type. batch_alter_table so this runs identically on
    # SQLite (table-recreate) and PostgreSQL (plain ALTER TABLE).
    # =========================================================================
    with op.batch_alter_table("task_runs") as batch_op:
        batch_op.add_column(sa.Column("source_task_run_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_task_runs_org_source_task_run",
            "task_runs",
            ["organization_id", "source_task_run_id"],
            ["organization_id", "id"],
            ondelete="RESTRICT",
        )

    # =========================================================================
    # cleaning_runs: one row per Module 6 cleaning TaskRun. status is a
    # plain string (not a native Postgres enum) -- see
    # app.models.cleaning_run's module docstring for the rationale.
    # =========================================================================
    op.create_table(
        "cleaning_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("task_run_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=False),
        sa.Column("source_task_run_id", sa.Uuid(), nullable=False),
        sa.Column("output_file_path", sa.String(length=1024), nullable=False),
        sa.Column("output_sha256", sa.String(length=64), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("total_changes_count", sa.Integer(), nullable=False),
        sa.Column("changes_by_rule", sa.JSON(), nullable=False),
        sa.Column("duplicate_row_count", sa.Integer(), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("post_clean_row_count", sa.Integer(), nullable=False),
        sa.Column("post_clean_missing_value_total", sa.Integer(), nullable=False),
        sa.Column("post_clean_duplicate_row_count", sa.Integer(), nullable=False),
        sa.Column("cleaning_engine_version", sa.String(length=20), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_cleaning_runs"),
        sa.UniqueConstraint("task_run_id", name="uq_cleaning_runs_task_run_id"),
        # Required so cleaning_changes can carry a tenant-aware composite FK
        # (organization_id, cleaning_run_id) -> (organization_id, id).
        sa.UniqueConstraint("organization_id", "id", name="uq_cleaning_runs_org_id"),
        sa.CheckConstraint(
            "status IN ('pending_review', 'approved', 'rejected', 'rolled_back')",
            name="ck_cleaning_runs_status_valid",
        ),
        sa.CheckConstraint("row_count >= 0", name="ck_cleaning_runs_row_count_nonnegative"),
        sa.CheckConstraint(
            "total_changes_count >= 0", name="ck_cleaning_runs_total_changes_nonnegative"
        ),
        sa.CheckConstraint(
            "duplicate_row_count >= 0", name="ck_cleaning_runs_duplicate_count_nonnegative"
        ),
        sa.CheckConstraint(
            "confidence_score >= 0 AND confidence_score <= 1",
            name="ck_cleaning_runs_confidence_range",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_cleaning_runs_organization_id_organizations", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_cleaning_runs_org_task_run", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_cleaning_runs_org_task", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_cleaning_runs_org_data_source", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "source_task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_cleaning_runs_org_source_task_run", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"], ["users.id"],
            name="fk_cleaning_runs_approved_by_users", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["rejected_by"], ["users.id"],
            name="fk_cleaning_runs_rejected_by_users", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["rolled_back_by"], ["users.id"],
            name="fk_cleaning_runs_rolled_back_by_users", ondelete="SET NULL",
        ),
    )
    op.create_index("ix_cleaning_runs_organization_id", "cleaning_runs", ["organization_id"])
    op.create_index("ix_cleaning_runs_task_run_id", "cleaning_runs", ["task_run_id"], unique=True)
    op.create_index("ix_cleaning_runs_task_id", "cleaning_runs", ["task_id"])
    op.create_index("ix_cleaning_runs_data_source_id", "cleaning_runs", ["data_source_id"])
    op.create_index(
        "ix_cleaning_runs_source_task_run_id", "cleaning_runs", ["source_task_run_id"]
    )

    # =========================================================================
    # cleaning_changes: append-only per-cell change log, capped per run at
    # settings.cleaning_max_persisted_changes (application-layer bound, not
    # a DB constraint -- same pattern as every other bounded/sampled table
    # in this project).
    # =========================================================================
    op.create_table(
        "cleaning_changes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("cleaning_run_id", sa.Uuid(), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("column_name", sa.String(length=255), nullable=False),
        sa.Column("original_value", sa.Text(), nullable=False),
        sa.Column("cleaned_value", sa.Text(), nullable=False),
        sa.Column("rule_name", sa.String(length=50), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_cleaning_changes"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_cleaning_changes_organization_id_organizations", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "cleaning_run_id"],
            ["cleaning_runs.organization_id", "cleaning_runs.id"],
            name="fk_cleaning_changes_org_cleaning_run", ondelete="CASCADE",
        ),
    )
    op.create_index("ix_cleaning_changes_organization_id", "cleaning_changes", ["organization_id"])
    op.create_index("ix_cleaning_changes_cleaning_run_id", "cleaning_changes", ["cleaning_run_id"])


def downgrade() -> None:
    op.drop_index("ix_cleaning_changes_cleaning_run_id", table_name="cleaning_changes")
    op.drop_index("ix_cleaning_changes_organization_id", table_name="cleaning_changes")
    op.drop_table("cleaning_changes")

    op.drop_index("ix_cleaning_runs_source_task_run_id", table_name="cleaning_runs")
    op.drop_index("ix_cleaning_runs_data_source_id", table_name="cleaning_runs")
    op.drop_index("ix_cleaning_runs_task_id", table_name="cleaning_runs")
    op.drop_index("ix_cleaning_runs_task_run_id", table_name="cleaning_runs")
    op.drop_index("ix_cleaning_runs_organization_id", table_name="cleaning_runs")
    op.drop_table("cleaning_runs")

    with op.batch_alter_table("task_runs") as batch_op:
        batch_op.drop_constraint("fk_task_runs_org_source_task_run", type_="foreignkey")
        batch_op.drop_column("source_task_run_id")
