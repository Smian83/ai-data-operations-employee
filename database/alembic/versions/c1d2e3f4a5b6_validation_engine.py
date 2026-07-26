"""validation engine

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5
Create Date: 2026-07-25 14:00:00.000000

Module 17 (Validation Engine) — Phase 1.

Adds:
  1. TaskType enum value 'validate' (PostgreSQL only -- SQLite uses plain
     VARCHAR with no migration-time enum change required, same reasoning
     as every prior enum-extension migration in this project).
  2. Table: validation_runs (one summary row per VALIDATE TaskRun).
  3. Table: validation_results (one result row per validated approved
     RemediationChange; append-only, never updated or deleted).
  4. Six indexes on validation_results, including ix_validation_results_outcome
     (Adjustment 3) and composite ix_validation_results_org_change_ts.

Purely additive -- no existing table, column, constraint, index, or enum
value is modified. The entire downgrade() undoes this migration by
dropping both new tables (and their indexes) in FK-safe order.

Design notes (see docs/module-17-validation-engine-design.md):

Adjustment 1 (snapshot freeze):
  Documented at the handler layer (Phase 3); no schema impact.

Adjustment 2 (validation_rule_version):
  validation_results carries both validation_engine_version (engine-level
  constant) and validation_rule_version (per-rule constant). The two can
  be bumped independently; storing both on every row makes per-rule
  changes auditable without joining to validation_runs.

Adjustment 3 (ix_validation_results_outcome):
  Dedicated index on validation_results.outcome to optimize the common
  API filters (?outcome=failed / ?outcome=passed / ?outcome=skipped).

Adjustment 4 (encryption compatibility):
  validation_results.reason is Text() (unbounded). Any future 'comment'
  or 'metadata' field added to this table must also use Text(), never
  VARCHAR. No encryption is implemented here.

FK ondelete strategy:
  organization_id -> organizations.id         CASCADE (org delete wipes tenant)
  (org, task_run_id) -> task_runs             CASCADE (run delete cascades)
  (org, task_id)    -> tasks                  RESTRICT (task delete blocked)
  (org, data_source_id) -> data_sources       RESTRICT (source delete blocked)
  (org, remediation_run_id) -> rem_runs       RESTRICT (audit row must survive)
  (org, validation_run_id) -> validation_runs CASCADE  (run delete cascades)
  remediation_change_id -> rem_changes.id     RESTRICT (audit row must survive)

Identifier length audit (all pre-measured, all <= 63 bytes):
  uq_validation_runs_task_run_id               30
  uq_validation_runs_org_id                    25
  fk_validation_runs_org                       22  (inline on column)
  fk_validation_runs_org_task_run              31
  fk_validation_runs_org_task                  27
  fk_validation_runs_org_data_source           35
  fk_validation_runs_org_remediation_run       41
  ck_validation_runs_considered_nonneg         38
  ck_validation_runs_passed_nonneg             29
  ck_validation_runs_failed_nonneg             29
  ck_validation_runs_skipped_nonneg            30
  ck_validation_runs_counts_reconcile          35
  ix_validation_runs_org                       22
  ix_validation_runs_task_run                  27
  ix_validation_runs_remediation_run           34
  uq_validation_results_org_id                 28
  fk_validation_results_org                    25  (inline on column)
  fk_validation_results_org_validation_run     41
  fk_validation_results_remediation_change     42
  ck_validation_results_outcome_valid          35
  ix_validation_results_org                    25
  ix_validation_results_run                    24
  ix_validation_results_remediation_run        37
  ix_validation_results_change                 28
  ix_validation_results_outcome                28
  ix_validation_results_org_change_ts          35
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c1d2e3f4a5b6"
down_revision: Union[str, None] = "b0c1d2e3f4a5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # =========================================================================
    # task_type_enum: add 'validate' to the existing native PostgreSQL enum
    # type. Fifth ALTER TYPE ... ADD VALUE in this project (after
    # d4e5f6a7b8c9's 'standardize', f1a2b3c4d5e6's 'match',
    # f7a8b9c0d1e2's 'detect', and a9b0c1d2e3f4's 'remediate') -- same
    # restriction applies: the new label cannot be used within the same
    # transaction it is added in, so this runs in its own autocommit block.
    #
    # SQLite has no native enum type and needs no equivalent change here --
    # tasks.task_type there is a plain VARCHAR with no CHECK constraint from
    # these migration-local enum objects (only the ORM model's own copy sets
    # create_constraint=True, and Alembic migrations never consult the ORM).
    # =========================================================================
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE task_type_enum ADD VALUE IF NOT EXISTS 'validate'")

    # =========================================================================
    # validation_runs: one immutable summary row per VALIDATE TaskRun.
    # Structural sibling of issue_detection_runs and remediation_runs --
    # no status/approval columns, since the engine never mutates anything
    # for a human to approve.
    # =========================================================================
    op.create_table(
        "validation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("task_run_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=False),
        sa.Column("remediation_run_id", sa.Uuid(), nullable=False),
        sa.Column("approved_changes_considered", sa.Integer(), nullable=False),
        sa.Column("passed_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("skipped_count", sa.Integer(), nullable=False),
        sa.Column("results_by_rule", sa.JSON(), nullable=False),
        sa.Column("validation_engine_version", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # ---- Primary key ----
        sa.PrimaryKeyConstraint("id"),
        # ---- Unique constraints ----
        sa.UniqueConstraint("task_run_id", name="uq_validation_runs_task_run_id"),
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_validation_runs_org_id"
        ),
        # ---- Check constraints ----
        sa.CheckConstraint(
            "approved_changes_considered >= 0",
            name="ck_validation_runs_considered_nonneg",
        ),
        sa.CheckConstraint(
            "passed_count >= 0",
            name="ck_validation_runs_passed_nonneg",
        ),
        sa.CheckConstraint(
            "failed_count >= 0",
            name="ck_validation_runs_failed_nonneg",
        ),
        sa.CheckConstraint(
            "skipped_count >= 0",
            name="ck_validation_runs_skipped_nonneg",
        ),
        sa.CheckConstraint(
            "passed_count + failed_count + skipped_count = approved_changes_considered",
            name="ck_validation_runs_counts_reconcile",
        ),
        # ---- FK → organizations (CASCADE) ----
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_validation_runs_org",
            ondelete="CASCADE",
        ),
        # ---- Composite FK → task_runs (CASCADE) ----
        sa.ForeignKeyConstraint(
            ["organization_id", "task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_validation_runs_org_task_run",
            ondelete="CASCADE",
        ),
        # ---- Composite FK → tasks (RESTRICT) ----
        sa.ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_validation_runs_org_task",
            ondelete="RESTRICT",
        ),
        # ---- Composite FK → data_sources (RESTRICT) ----
        sa.ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_validation_runs_org_data_source",
            ondelete="RESTRICT",
        ),
        # ---- Composite FK → remediation_runs (RESTRICT) ----
        sa.ForeignKeyConstraint(
            ["organization_id", "remediation_run_id"],
            ["remediation_runs.organization_id", "remediation_runs.id"],
            name="fk_validation_runs_org_remediation_run",
            ondelete="RESTRICT",
        ),
    )

    # ---- validation_runs scalar indexes ----
    op.create_index(
        "ix_validation_runs_org",
        "validation_runs",
        ["organization_id"],
    )
    op.create_index(
        "ix_validation_runs_task_run",
        "validation_runs",
        ["task_run_id"],
    )
    op.create_index(
        "ix_validation_runs_remediation_run",
        "validation_runs",
        ["remediation_run_id"],
    )

    # =========================================================================
    # validation_results: one result row per approved RemediationChange
    # evaluated by a ValidationRun. Append-only, immutable.
    #
    # Two version columns (Adjustment 2):
    #   validation_engine_version: engine-level constant, same across all
    #     rules in a single run.
    #   validation_rule_version: per-rule constant, independent of engine
    #     version. Allows auditing per-rule logic changes at the row level.
    #
    # reason is Text() for encryption compatibility (Adjustment 4).
    # ix_validation_results_outcome is Adjustment 3.
    #
    # No UNIQUE(organization_id, remediation_change_id): deliberate omission.
    # A future re-validation pass inserts new rows without erasing history.
    # =========================================================================
    op.create_table(
        "validation_results",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("validation_run_id", sa.Uuid(), nullable=False),
        sa.Column("remediation_run_id", sa.Uuid(), nullable=False),
        sa.Column("remediation_change_id", sa.Uuid(), nullable=False),
        sa.Column("source_issue_id", sa.Uuid(), nullable=False),
        sa.Column("validation_rule", sa.String(length=50), nullable=False),
        sa.Column("outcome", sa.String(length=10), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),       # Text() for Adjustment 4
        sa.Column("original_value", sa.Text(), nullable=True),
        sa.Column("proposed_value", sa.Text(), nullable=True),
        sa.Column("validation_engine_version", sa.String(length=20), nullable=False),
        sa.Column("validation_rule_version", sa.String(length=20), nullable=False),  # Adjustment 2
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # ---- Primary key ----
        sa.PrimaryKeyConstraint("id"),
        # ---- Unique constraint (org_id, id) for future composite FK target ----
        sa.UniqueConstraint(
            "organization_id", "id", name="uq_validation_results_org_id"
        ),
        # ---- CHECK: outcome must be one of the three known values ----
        sa.CheckConstraint(
            "outcome IN ('passed', 'failed', 'skipped')",
            name="ck_validation_results_outcome_valid",
        ),
        # ---- FK → organizations (CASCADE) ----
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_validation_results_org",
            ondelete="CASCADE",
        ),
        # ---- Composite FK → validation_runs (CASCADE) ----
        sa.ForeignKeyConstraint(
            ["organization_id", "validation_run_id"],
            ["validation_runs.organization_id", "validation_runs.id"],
            name="fk_validation_results_org_validation_run",
            ondelete="CASCADE",
        ),
        # ---- Simple FK → remediation_changes.id (RESTRICT) ----
        # Note: remediation_changes has no UNIQUE(organization_id, id),
        # so a composite FK is not possible -- same situation and reasoning
        # as remediation_change_decisions. Tenant isolation is enforced by
        # organization_id -> organizations.id above.
        sa.ForeignKeyConstraint(
            ["remediation_change_id"],
            ["remediation_changes.id"],
            name="fk_validation_results_remediation_change",
            ondelete="RESTRICT",
        ),
    )

    # ---- validation_results scalar indexes ----
    op.create_index(
        "ix_validation_results_org",
        "validation_results",
        ["organization_id"],
    )
    op.create_index(
        "ix_validation_results_run",
        "validation_results",
        ["validation_run_id"],
    )
    op.create_index(
        "ix_validation_results_remediation_run",
        "validation_results",
        ["remediation_run_id"],
    )
    op.create_index(
        "ix_validation_results_change",
        "validation_results",
        ["remediation_change_id"],
    )

    # Adjustment 3: dedicated outcome index for API filters (?outcome=...)
    op.create_index(
        "ix_validation_results_outcome",
        "validation_results",
        ["outcome"],
    )

    # Composite index: supports "latest result per change" queries if a
    # future re-validation pass exists, and run-scoped batch reads.
    op.create_index(
        "ix_validation_results_org_change_ts",
        "validation_results",
        ["organization_id", "remediation_change_id", "created_at"],
    )


def downgrade() -> None:
    # Drop validation_results indexes and table first (child of validation_runs)
    op.drop_index(
        "ix_validation_results_org_change_ts",
        table_name="validation_results",
    )
    op.drop_index(
        "ix_validation_results_outcome",
        table_name="validation_results",
    )
    op.drop_index(
        "ix_validation_results_change",
        table_name="validation_results",
    )
    op.drop_index(
        "ix_validation_results_remediation_run",
        table_name="validation_results",
    )
    op.drop_index(
        "ix_validation_results_run",
        table_name="validation_results",
    )
    op.drop_index(
        "ix_validation_results_org",
        table_name="validation_results",
    )
    op.drop_table("validation_results")

    # Drop validation_runs indexes and table
    op.drop_index(
        "ix_validation_runs_remediation_run",
        table_name="validation_runs",
    )
    op.drop_index(
        "ix_validation_runs_task_run",
        table_name="validation_runs",
    )
    op.drop_index(
        "ix_validation_runs_org",
        table_name="validation_runs",
    )
    op.drop_table("validation_runs")

    # Note: ALTER TYPE ... DROP VALUE is not supported by PostgreSQL.
    # 'validate' added to task_type_enum in upgrade() is permanent at the
    # database layer. The ORM and application layer will simply never create
    # a TaskRun with task_type='validate' after this migration is rolled
    # back. Same documented constraint as every prior enum-extension migration
    # in this project (d4e5f6a7b8c9, f1a2b3c4d5e6, f7a8b9c0d1e2, a9b0c1d2e3f4).
