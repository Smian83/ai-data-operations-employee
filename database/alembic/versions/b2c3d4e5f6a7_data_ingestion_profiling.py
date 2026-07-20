"""data ingestion and profiling engine

Revision ID: b2c3d4e5f6a7
Revises: a1c2d4f6b8e0
Create Date: 2026-07-20 14:30:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b2c3d4e5f6a7"
down_revision: Union[str, None] = "a1c2d4f6b8e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "data_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("task_run_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=False),
        sa.Column("source_filename", sa.String(length=1024), nullable=False),
        sa.Column("source_size_bytes", sa.Integer(), nullable=False),
        sa.Column("source_sha256", sa.String(length=64), nullable=False),
        sa.Column("detected_encoding", sa.String(length=50), nullable=False),
        sa.Column("delimiter", sa.String(length=8), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("column_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_row_count", sa.Integer(), nullable=False),
        sa.Column("missing_value_total", sa.Integer(), nullable=False),
        sa.Column("column_profiles", sa.JSON(), nullable=False),
        sa.Column("structural_issues", sa.JSON(), nullable=False),
        sa.Column("limits_applied", sa.JSON(), nullable=False),
        sa.Column(
            "profiled_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_data_profiles"),
        sa.UniqueConstraint("task_run_id", name="uq_data_profiles_task_run_id"),
        sa.CheckConstraint(
            "source_size_bytes >= 0",
            name="ck_data_profiles_source_size_nonnegative",
        ),
        sa.CheckConstraint(
            "row_count >= 0",
            name="ck_data_profiles_row_count_nonnegative",
        ),
        sa.CheckConstraint(
            "column_count > 0",
            name="ck_data_profiles_column_count_positive",
        ),
        sa.CheckConstraint(
            "duplicate_row_count >= 0 AND duplicate_row_count <= row_count",
            name="ck_data_profiles_duplicate_count_valid",
        ),
        sa.CheckConstraint(
            "missing_value_total >= 0",
            name="ck_data_profiles_missing_total_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_data_profiles_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_data_profiles_org_task_run",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_data_profiles_org_task",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_data_profiles_org_data_source",
            ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_data_profiles_organization_id",
        "data_profiles",
        ["organization_id"],
    )
    op.create_index(
        "ix_data_profiles_task_run_id",
        "data_profiles",
        ["task_run_id"],
    )
    op.create_index("ix_data_profiles_task_id", "data_profiles", ["task_id"])
    op.create_index(
        "ix_data_profiles_data_source_id",
        "data_profiles",
        ["data_source_id"],
    )


def downgrade() -> None:
    op.drop_index("ix_data_profiles_data_source_id", table_name="data_profiles")
    op.drop_index("ix_data_profiles_task_id", table_name="data_profiles")
    op.drop_index("ix_data_profiles_task_run_id", table_name="data_profiles")
    op.drop_index("ix_data_profiles_organization_id", table_name="data_profiles")
    op.drop_table("data_profiles")
