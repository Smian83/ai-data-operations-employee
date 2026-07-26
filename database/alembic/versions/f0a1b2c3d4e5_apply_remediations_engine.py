"""apply remediations engine

Revision ID: f0a1b2c3d4e5
Revises: e3f4a5b6c7d8
Create Date: 2026-07-26 12:00:00.000000

APPLY_REMEDIATIONS engine — Phase 1.

Adds:
  1. TaskType enum value 'apply_remediations' (PostgreSQL only -- SQLite uses
     plain VARCHAR with no migration-time enum change required, same reasoning
     as every prior enum-extension migration in this project).
  2. Table: applied_remediation_runs (one row per APPLY_REMEDIATIONS TaskRun;
     immutable after creation; stores output_file_path, output_sha256,
     decisions_snapshot_hash, applied_change_count, skipped_change_count,
     apply_engine_version).
  3. Column: validation_runs.applied_remediation_run_id (nullable UUID FK to
     applied_remediation_runs via composite FK (organization_id, id)).

Purely additive -- no existing table, column, constraint, index, or enum
value is modified (except for the additive column on validation_runs). The
entire downgrade() drops the column, then the table.

Key design decisions:

applied_remediation_runs:
  - No status column (same as RemediationRun, IssueDetectionRun, ValidationRun).
    The row IS the completed record. A failed run has no row.
  - UNIQUE(task_run_id): idempotency gate. IntegrityError on duplicate triggers
    the handler's catch-and-refetch path.
  - UNIQUE(organization_id, id): required so downstream FKs (e.g., from
    validation_runs) can use composite FK (organization_id, id).
  - remediation_run_id FK (RESTRICT): audit preservation -- an applied run row
    must not silently disappear if the upstream RemediationRun is removed.

validation_runs.applied_remediation_run_id:
  - Nullable: existing ValidationRun rows (from before this migration) have no
    linked AppliedRemediationRun. Set only when ValidationHandler chains from
    an APPLY_REMEDIATIONS TaskRun.
  - FK: RESTRICT, via composite FK (organization_id, applied_remediation_run_id)
    -> (applied_remediation_runs.organization_id, applied_remediation_runs.id).

Identifier length audit (all <= 63 bytes):
  uq_applied_remediation_runs_task_run_id          40
  uq_applied_remediation_runs_org_id               34
  fk_applied_remediation_runs_org_task_run         39
  fk_applied_remediation_runs_org_task             35
  fk_applied_remediation_runs_org_data_source      44
  fk_applied_remediation_runs_org_remediation_run  48
  ck_applied_remediation_runs_applied_count_nonneg 48
  ck_applied_remediation_runs_skipped_count_nonneg 48
  ix_applied_remediation_runs_org                  31
  ix_applied_remediation_runs_data_source          39
  ix_applied_remediation_runs_remediation_run      42
  fk_validation_runs_org_applied_remediation_run   46
  ix_validation_runs_applied_remediation_run_id    45
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f0a1b2c3d4e5"
down_revision: Union[str, None] = "e3f4a5b6c7d8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # =========================================================================
    # task_type_enum: add 'apply_remediations' to the existing native
    # PostgreSQL enum type. Eighth ALTER TYPE ... ADD VALUE in this project
    # (after d4e5f6a7b8c9's 'standardize', f1a2b3c4d5e6's 'match',
    # f7a8b9c0d1e2's 'detect', a9b0c1d2e3f4's 'remediate',
    # c1d2e3f4a5b6's 'validate', d2e3f4a5b6c7's 'quality_ctrl', and
    # e3f4a5b6c7d8's 'clean_export').
    # Same restriction applies: the new label cannot be used within the
    # same transaction it is added in, so this runs in its own autocommit block.
    # =========================================================================
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE task_type_enum ADD VALUE IF NOT EXISTS 'apply_remediations'"
            )

    # =========================================================================
    # applied_remediation_runs: one row per APPLY_REMEDIATIONS TaskRun.
    #
    # Immutable after the single write transaction -- same pattern as
    # IssueDetectionRun, RemediationRun, ValidationRun, and QualityControlRun.
    # No status column: the row IS the completed record. A failed run has no row.
    #
    # decisions_snapshot_hash: SHA-256 of the ordered, deterministic
    #   representation of the applied-decision set. Used by Module 19 Gate 4.
    #
    # output_file_path / output_sha256: the materialized CSV artifact location
    #   and its SHA-256 checksum. Same convention as ExportRun.
    # =========================================================================
    op.create_table(
        "applied_remediation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("task_run_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=False),
        sa.Column("remediation_run_id", sa.Uuid(), nullable=False),
        sa.Column("output_file_path", sa.Text(), nullable=False),
        sa.Column("output_sha256", sa.String(length=64), nullable=False),
        sa.Column("decisions_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("applied_change_count", sa.Integer(), nullable=False),
        sa.Column("skipped_change_count", sa.Integer(), nullable=False),
        sa.Column("apply_engine_version", sa.String(length=20), nullable=False),
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
            name="uq_applied_remediation_runs_task_run_id",
        ),
        sa.UniqueConstraint(
            "organization_id", "id",
            name="uq_applied_remediation_runs_org_id",
        ),
        # ---- Check constraints ----
        sa.CheckConstraint(
            "applied_change_count >= 0",
            name="ck_applied_remediation_runs_applied_count_nonneg",
        ),
        sa.CheckConstraint(
            "skipped_change_count >= 0",
            name="ck_applied_remediation_runs_skipped_count_nonneg",
        ),
        # ---- FK → organizations (CASCADE) ----
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_applied_remediation_runs_org",
            ondelete="CASCADE",
        ),
        # ---- Composite FK → task_runs (CASCADE) ----
        sa.ForeignKeyConstraint(
            ["organization_id", "task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_applied_remediation_runs_org_task_run",
            ondelete="CASCADE",
        ),
        # ---- Composite FK → tasks (RESTRICT) ----
        sa.ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_applied_remediation_runs_org_task",
            ondelete="RESTRICT",
        ),
        # ---- Composite FK → data_sources (RESTRICT) ----
        sa.ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_applied_remediation_runs_org_data_source",
            ondelete="RESTRICT",
        ),
        # ---- Composite FK → remediation_runs (RESTRICT) ----
        sa.ForeignKeyConstraint(
            ["organization_id", "remediation_run_id"],
            ["remediation_runs.organization_id", "remediation_runs.id"],
            name="fk_applied_remediation_runs_org_remediation_run",
            ondelete="RESTRICT",
        ),
    )

    # ---- applied_remediation_runs indexes ----
    op.create_index(
        "ix_applied_remediation_runs_org",
        "applied_remediation_runs",
        ["organization_id"],
    )
    op.create_index(
        "ix_applied_remediation_runs_data_source",
        "applied_remediation_runs",
        ["data_source_id"],
    )
    op.create_index(
        "ix_applied_remediation_runs_remediation_run",
        "applied_remediation_runs",
        ["remediation_run_id"],
    )

    # =========================================================================
    # validation_runs.applied_remediation_run_id:
    #   Nullable FK to the AppliedRemediationRun whose materialized artifact
    #   was the source for this validation run. NULL for ValidationRun rows
    #   created before APPLY_REMEDIATIONS existed (they chained from
    #   RemediationRun directly). Set by ValidationHandler when it resolves
    #   an AppliedRemediationRun as source_task_run_id.
    # =========================================================================
    op.add_column(
        "validation_runs",
        sa.Column("applied_remediation_run_id", sa.Uuid(), nullable=True),
    )
    op.create_index(
        "ix_validation_runs_applied_remediation_run_id",
        "validation_runs",
        ["applied_remediation_run_id"],
    )
    # Composite FK constraint is only supported on PostgreSQL -- SQLite does
    # not support ALTER TABLE ADD CONSTRAINT. Application-layer referential
    # integrity (ValidationHandler only ever writes valid applied_remediation_run_id
    # values) is sufficient for the SQLite/test environment. Same precedent as
    # every prior migration that gates DDL behind a dialect check.
    if bind.dialect.name == "postgresql":
        op.create_foreign_key(
            "fk_validation_runs_org_applied_remediation_run",
            "validation_runs",
            "applied_remediation_runs",
            ["organization_id", "applied_remediation_run_id"],
            ["organization_id", "id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    # Drop validation_runs.applied_remediation_run_id FK + index + column.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.drop_constraint(
            "fk_validation_runs_org_applied_remediation_run",
            "validation_runs",
            type_="foreignkey",
        )
    op.drop_index(
        "ix_validation_runs_applied_remediation_run_id",
        table_name="validation_runs",
    )
    op.drop_column("validation_runs", "applied_remediation_run_id")

    # Drop applied_remediation_runs indexes, then the table.
    op.drop_index("ix_applied_remediation_runs_remediation_run",
                  table_name="applied_remediation_runs")
    op.drop_index("ix_applied_remediation_runs_data_source",
                  table_name="applied_remediation_runs")
    op.drop_index("ix_applied_remediation_runs_org",
                  table_name="applied_remediation_runs")
    op.drop_table("applied_remediation_runs")

    # Note: ALTER TYPE ... DROP VALUE is not supported by PostgreSQL.
    # 'apply_remediations' added to task_type_enum in upgrade() is permanent
    # at the database layer. The ORM and application layer will simply never
    # create a TaskRun with task_type='apply_remediations' after this migration
    # is rolled back. Same documented constraint as every prior enum-extension
    # migration in this project.
