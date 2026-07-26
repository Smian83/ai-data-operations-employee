"""reports engine

Revision ID: 1b2c3d4e5f6a
Revises: f0a1b2c3d4e5
Create Date: 2026-07-26 14:00:00.000000

Module 20 — Reports & Analytics engine.

Adds:
  1. TaskType enum value 'report' (PostgreSQL only — SQLite uses plain VARCHAR
     with no migration-time enum change required, same reasoning as every prior
     enum-extension migration in this project).
  2. Table: report_runs (one row per REPORT TaskRun; immutable after creation;
     stores the full structured pipeline report JSON in the report_data column.
     No file artifact, no file root config — the JSON is stored inline, same
     pattern as QualityControlRun.execution_snapshot).

Purely additive — no existing table, column, constraint, index, or enum value
is modified. The entire downgrade() drops indexes then the table.

Key design decisions:

report_runs:
  - No status column (same as IssueDetectionRun, RemediationRun, ValidationRun,
    QualityControlRun). The row IS the completed record. A failed run has no row.
  - UNIQUE(task_run_id): idempotency gate. IntegrityError on duplicate triggers
    the handler's catch-and-refetch path.
  - UNIQUE(organization_id, id): required so downstream FKs (if any future module
    needs them) can use composite FK (organization_id, id).
  - quality_control_run_id FK (RESTRICT, PostgreSQL only via op.create_foreign_key):
    same precedent as validation_runs.applied_remediation_run_id -- SQLite does not
    support ALTER TABLE ADD CONSTRAINT, so application-layer referential integrity
    (handler always writes valid IDs) is sufficient for the test environment.
  - quality_control_run_id is nullable: NULL for partial-pipeline reports where
    no QualityControlRun exists yet.
  - report_data: JSON column storing the complete structured report. Serialized to
    bytes by the download endpoint. Schema version embedded inside JSON under
    report_schema_version.

Identifier length audit (all <= 63 bytes):
  uq_report_runs_task_run_id          26
  uq_report_runs_org_id               22
  fk_report_runs_org_task_run         27
  fk_report_runs_org_task             23
  fk_report_runs_org_data_source      30
  fk_report_runs_org                  18
  fk_report_runs_org_quality_control  34
  ix_report_runs_org                  18
  ix_report_runs_task_id              22
  ix_report_runs_data_source          26
  ix_report_runs_quality_control      30
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "1b2c3d4e5f6a"
down_revision: Union[str, None] = "f0a1b2c3d4e5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # =========================================================================
    # task_type_enum: add 'report' to the existing native PostgreSQL enum type.
    # Ninth ALTER TYPE ... ADD VALUE in this project (after 'standardize',
    # 'match', 'detect', 'remediate', 'validate', 'quality_ctrl',
    # 'clean_export', and 'apply_remediations').
    # Same restriction applies: the new label cannot be used within the same
    # transaction it is added in, so this runs in its own autocommit block.
    # =========================================================================
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE task_type_enum ADD VALUE IF NOT EXISTS 'report'"
            )

    # =========================================================================
    # report_runs: one row per REPORT TaskRun.
    #
    # Immutable after the single write transaction -- same pattern as
    # IssueDetectionRun, RemediationRun, ValidationRun, QualityControlRun,
    # and AppliedRemediationRun. No status column: the row IS the completed
    # record. A failed or pending run has no row.
    #
    # report_data: full structured pipeline report JSON -- schema version is
    # embedded inside the JSON under report_schema_version. No separate file
    # artifact. The download endpoint serializes this column to bytes on demand.
    #
    # quality_control_run_id: nullable UUID referencing the QualityControlRun
    # that anchored the report. NULL for partial-pipeline reports. The FK to
    # quality_control_runs is PostgreSQL-only (see op.create_foreign_key below).
    # =========================================================================
    op.create_table(
        "report_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("task_run_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=False),
        sa.Column("quality_control_run_id", sa.Uuid(), nullable=True),
        sa.Column("report_engine_version", sa.String(length=20), nullable=False),
        sa.Column("report_schema_version", sa.String(length=10), nullable=False),
        sa.Column("report_data", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # ---- Primary key ----
        sa.PrimaryKeyConstraint("id"),
        # ---- Unique constraints ----
        sa.UniqueConstraint(
            "task_run_id",
            name="uq_report_runs_task_run_id",
        ),
        sa.UniqueConstraint(
            "organization_id", "id",
            name="uq_report_runs_org_id",
        ),
        # ---- FK → organizations (CASCADE) ----
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_report_runs_org",
            ondelete="CASCADE",
        ),
        # ---- Composite FK → task_runs (CASCADE) ----
        sa.ForeignKeyConstraint(
            ["organization_id", "task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_report_runs_org_task_run",
            ondelete="CASCADE",
        ),
        # ---- Composite FK → tasks (RESTRICT) ----
        sa.ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_report_runs_org_task",
            ondelete="RESTRICT",
        ),
        # ---- Composite FK → data_sources (RESTRICT) ----
        sa.ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_report_runs_org_data_source",
            ondelete="RESTRICT",
        ),
    )

    # ---- report_runs indexes ----
    op.create_index(
        "ix_report_runs_org",
        "report_runs",
        ["organization_id"],
    )
    op.create_index(
        "ix_report_runs_task_id",
        "report_runs",
        ["task_id"],
    )
    op.create_index(
        "ix_report_runs_data_source",
        "report_runs",
        ["data_source_id"],
    )
    op.create_index(
        "ix_report_runs_quality_control",
        "report_runs",
        ["quality_control_run_id"],
    )

    # =========================================================================
    # quality_control_run_id FK: composite FK to quality_control_runs.
    # PostgreSQL-only -- SQLite does not support ALTER TABLE ADD CONSTRAINT.
    # Application-layer referential integrity (ReportHandler only ever writes
    # valid quality_control_run_id values) is sufficient for the test
    # environment. Same precedent as:
    #   - validation_runs.applied_remediation_run_id (f0a1b2c3d4e5)
    # =========================================================================
    if bind.dialect.name == "postgresql":
        op.create_foreign_key(
            "fk_report_runs_org_quality_control",
            "report_runs",
            "quality_control_runs",
            ["organization_id", "quality_control_run_id"],
            ["organization_id", "id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    # Drop quality_control_run_id FK (PostgreSQL only).
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_constraint(
            "fk_report_runs_org_quality_control",
            "report_runs",
            type_="foreignkey",
        )

    # Drop report_runs indexes, then the table.
    op.drop_index("ix_report_runs_quality_control", table_name="report_runs")
    op.drop_index("ix_report_runs_data_source", table_name="report_runs")
    op.drop_index("ix_report_runs_task_id", table_name="report_runs")
    op.drop_index("ix_report_runs_org", table_name="report_runs")
    op.drop_table("report_runs")

    # Note: ALTER TYPE ... DROP VALUE is not supported by PostgreSQL.
    # 'report' added to task_type_enum in upgrade() is permanent at the
    # database layer. The ORM and application layer will simply never create
    # a TaskRun with task_type='report' after this migration is rolled back.
    # Same documented constraint as every prior enum-extension migration.
