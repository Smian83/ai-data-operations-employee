"""clean export engine

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-07-26 10:00:00.000000

Module 19 (Clean Export Engine) — Phase 1.

Adds:
  1. TaskType enum value 'clean_export' (PostgreSQL only -- SQLite uses plain
     VARCHAR with no migration-time enum change required, same reasoning as
     every prior enum-extension migration in this project).
  2. Table: clean_exports (one row per clean export request; idempotency via
     UNIQUE(idempotency_key); stores artifact_id for path construction,
     checksum for integrity, row/column counts, status lifecycle).

Purely additive -- no existing table, column, constraint, index, or enum
value is modified. The entire downgrade() undoes this migration by dropping
the clean_exports table (and its indexes).

Key design decisions (see docs/module-19-clean-export-engine-design.md):

status lifecycle:
  pending    → created but not yet processed (async path)
  processing → actively writing the artifact
  completed  → artifact written, checksum stored, download available
  failed     → export attempt failed (failure_reason set)
  blocked    → dataset ineligible (eligibility check failed)
  expired    → artifact deleted by retention; metadata row remains

idempotency_key UNIQUE constraint:
  Callers supply a stable idempotency_key. A second request with the same
  key returns the existing row. Enforced here by uq_clean_exports_idempotency_key.

artifact_id:
  Server-generated UUID used to construct the artifact file path:
    {CLEAN_EXPORT_OUTPUT_ROOT}/{organization_id}/{artifact_id}.{format}
  NULL for non-completed rows.

dataset_version:
  The QualityControlRun.id that authorized this export. Nullable on blocked
  rows where no passing QC run exists.

FK ondelete strategy:
  organization_id -> organizations.id           CASCADE (org delete wipes tenant)
  (org, job_id)   -> tasks                      RESTRICT (task delete blocked)
  (org, data_source_id) -> data_sources         RESTRICT (source delete blocked)

No FK to quality_control_runs for dataset_version:
  quality_control_runs has UNIQUE(organization_id, id) which would allow a
  composite FK. However, dataset_version is nullable (blocked rows have no
  passing QC run), and the QC check is a business-logic gate, not a DB-enforced
  integrity boundary. A simple application-layer reference is used instead,
  consistent with this module's read-only-audit approach.

Identifier length audit (all pre-measured, all <= 63 bytes):
  fk_clean_exports_org                          24  (inline on column)
  fk_clean_exports_org_job                      24
  fk_clean_exports_org_data_source              32
  uq_clean_exports_idempotency_key              32
  uq_clean_exports_org_id                       23
  ck_clean_exports_status_valid                 29
  ck_clean_exports_format_valid                 29
  ck_clean_exports_row_count_nonneg             30
  ck_clean_exports_column_count_min             30
  ix_clean_exports_org                          20
  ix_clean_exports_job                          20
  ix_clean_exports_data_source                  27
  ix_clean_exports_status                       24
  ix_clean_exports_dataset_version              31
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e3f4a5b6c7d8"
down_revision: Union[str, None] = "d2e3f4a5b6c7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # =========================================================================
    # task_type_enum: add 'clean_export' to the existing native PostgreSQL
    # enum type. Seventh ALTER TYPE ... ADD VALUE in this project (after
    # d4e5f6a7b8c9's 'standardize', f1a2b3c4d5e6's 'match',
    # f7a8b9c0d1e2's 'detect', a9b0c1d2e3f4's 'remediate',
    # c1d2e3f4a5b6's 'validate', and d2e3f4a5b6c7's 'quality_ctrl').
    # Same restriction applies: the new label cannot be used within the
    # same transaction it is added in, so this runs in its own autocommit block.
    # =========================================================================
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE task_type_enum ADD VALUE IF NOT EXISTS 'clean_export'"
            )

    # =========================================================================
    # clean_exports: one row per clean export request.
    #
    # idempotency_key UNIQUE:
    #   Enforces "one completed export per caller-supplied key". A second
    #   request with the same key returns the existing row. The application
    #   layer performs an early-exit SELECT before INSERT, then catches any
    #   concurrent-duplicate IntegrityError with catch-and-refetch.
    #
    # status closed vocabulary enforced by CHECK constraint:
    #   pending | processing | completed | failed | blocked | expired
    #
    # format closed vocabulary enforced by CHECK constraint:
    #   csv | xlsx
    #
    # artifact_id: NULL for non-completed rows. Populated on completion with
    #   a server-generated UUID used to construct the on-disk path.
    #
    # dataset_version: QualityControlRun.id that authorized this export.
    #   NULL for blocked rows. No FK constraint (see module docstring).
    # =========================================================================
    op.create_table(
        "clean_exports",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        # job_id = task_id in this project (Tasks ARE jobs)
        sa.Column("job_id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=False),
        # Nullable: NULL for blocked rows where no passing QC run exists.
        sa.Column("dataset_version", sa.Uuid(), nullable=True),
        sa.Column("format", sa.String(length=10), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        # Server-generated UUID for the artifact file path. NULL pre-completion.
        sa.Column("artifact_id", sa.Uuid(), nullable=True),
        # SHA-256 hex digest of artifact bytes. NULL pre-completion.
        sa.Column("checksum", sa.String(length=64), nullable=True),
        # Row and column counts. NULL pre-completion.
        sa.Column("row_count", sa.Integer(), nullable=True),
        sa.Column("column_count", sa.Integer(), nullable=True),
        # Caller-supplied idempotency key. UNIQUE.
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        # NULL for completed rows.
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Set when status transitions to completed/failed/blocked.
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        # ---- Primary key ----
        sa.PrimaryKeyConstraint("id"),
        # ---- Unique constraints ----
        sa.UniqueConstraint(
            "idempotency_key",
            name="uq_clean_exports_idempotency_key",
        ),
        sa.UniqueConstraint(
            "organization_id", "id",
            name="uq_clean_exports_org_id",
        ),
        # ---- Check constraints ----
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'completed', 'failed', "
            "'blocked', 'expired')",
            name="ck_clean_exports_status_valid",
        ),
        sa.CheckConstraint(
            "format IN ('csv', 'xlsx')",
            name="ck_clean_exports_format_valid",
        ),
        sa.CheckConstraint(
            "row_count IS NULL OR row_count >= 0",
            name="ck_clean_exports_row_count_nonneg",
        ),
        sa.CheckConstraint(
            "column_count IS NULL OR column_count >= 1",
            name="ck_clean_exports_column_count_min",
        ),
        # ---- FK → organizations (CASCADE) ----
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_clean_exports_org",
            ondelete="CASCADE",
        ),
        # ---- Composite FK → tasks (RESTRICT) ----
        sa.ForeignKeyConstraint(
            ["organization_id", "job_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_clean_exports_org_job",
            ondelete="RESTRICT",
        ),
        # ---- Composite FK → data_sources (RESTRICT) ----
        sa.ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_clean_exports_org_data_source",
            ondelete="RESTRICT",
        ),
    )

    # ---- clean_exports scalar indexes ----
    op.create_index(
        "ix_clean_exports_org",
        "clean_exports",
        ["organization_id"],
    )
    op.create_index(
        "ix_clean_exports_job",
        "clean_exports",
        ["job_id"],
    )
    op.create_index(
        "ix_clean_exports_data_source",
        "clean_exports",
        ["data_source_id"],
    )
    op.create_index(
        "ix_clean_exports_status",
        "clean_exports",
        ["status"],
    )
    op.create_index(
        "ix_clean_exports_dataset_version",
        "clean_exports",
        ["dataset_version"],
    )


def downgrade() -> None:
    # Drop clean_exports indexes first, then the table.
    op.drop_index("ix_clean_exports_dataset_version", table_name="clean_exports")
    op.drop_index("ix_clean_exports_status", table_name="clean_exports")
    op.drop_index("ix_clean_exports_data_source", table_name="clean_exports")
    op.drop_index("ix_clean_exports_job", table_name="clean_exports")
    op.drop_index("ix_clean_exports_org", table_name="clean_exports")
    op.drop_table("clean_exports")

    # Note: ALTER TYPE ... DROP VALUE is not supported by PostgreSQL.
    # 'clean_export' added to task_type_enum in upgrade() is permanent at
    # the database layer. The ORM and application layer will simply never
    # create a TaskRun with task_type='clean_export' after this migration
    # is rolled back. Same documented constraint as every prior enum-extension
    # migration in this project.
