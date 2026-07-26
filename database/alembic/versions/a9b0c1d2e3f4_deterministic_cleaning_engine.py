"""deterministic cleaning engine

Revision ID: a9b0c1d2e3f4
Revises: f7a8b9c0d1e2
Create Date: 2026-07-24 13:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a9b0c1d2e3f4"
down_revision: Union[str, None] = "f7a8b9c0d1e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # =========================================================================
    # task_type_enum: add 'remediate' to the existing native PostgreSQL enum
    # type. Fourth ALTER TYPE ... ADD VALUE in this project (after
    # d4e5f6a7b8c9's 'standardize', f1a2b3c4d5e6's 'match', and
    # f7a8b9c0d1e2's 'detect') -- same restriction applies: the new label
    # cannot be used within the same transaction it is added in, so this
    # runs in its own autocommit block.
    #
    # SQLite has no native enum type and needs no equivalent change here --
    # same reasoning as every prior enum-extension migration in this
    # project.
    # =========================================================================
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE task_type_enum ADD VALUE IF NOT EXISTS 'remediate'")

    # =========================================================================
    # Module 14 additive touch 1: issue_detection_runs.source_sha256
    # (nullable). Lets Module 15's RemediationHandler verify it is
    # remediating the exact same dataset version Module 14 scanned. Existing
    # rows predate this column and are NULL -- treated as a permanent
    # failure ("re-run detection") by the remediation handler, never a
    # skipped or passed check. See
    # docs/module-15-deterministic-cleaning-engine-design.md Section 2.
    # =========================================================================
    op.add_column(
        "issue_detection_runs",
        sa.Column("source_sha256", sa.String(length=64), nullable=True),
    )

    # =========================================================================
    # Module 14 additive touch 2: UniqueConstraint(organization_id, id) on
    # issues. Required so remediation_changes.source_issue_id can be a
    # composite FK (organization_id, source_issue_id) ->
    # (organization_id, id) -- this project's own established rule (learned
    # in Module 6: add the constraint at the same time a table is created,
    # not after the first FK-target failure surfaces). Purely additive.
    #
    # batch_alter_table: on PostgreSQL this compiles to a plain ALTER TABLE
    # ADD CONSTRAINT; SQLite cannot ADD a constraint to an existing table in
    # place at all, so Alembic transparently recreates the table instead --
    # same established pattern as a1c2d4f6b8e0's own
    # uq_task_runs_org_id addition.
    # =========================================================================
    with op.batch_alter_table("issues") as batch_op:
        batch_op.create_unique_constraint(
            "uq_issues_org_id", ["organization_id", "id"]
        )

    # =========================================================================
    # remediation_runs: one row per Module 15 REMEDIATE TaskRun. Structural
    # sibling of issue_detection_runs -- no status/approval columns, since
    # the engine only ever persists proposals, never a materialized output.
    # =========================================================================
    op.create_table(
        "remediation_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("task_run_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=False),
        sa.Column("source_task_run_id", sa.Uuid(), nullable=False),
        sa.Column("issues_considered_count", sa.Integer(), nullable=False),
        sa.Column("total_changes_count", sa.Integer(), nullable=False),
        sa.Column("issues_skipped_count", sa.Integer(), nullable=False),
        sa.Column("changes_by_action", sa.JSON(), nullable=False),
        sa.Column("skipped_by_reason", sa.JSON(), nullable=False),
        sa.Column("remediation_engine_version", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_remediation_runs"),
        sa.UniqueConstraint("task_run_id", name="uq_remediation_runs_task_run_id"),
        sa.UniqueConstraint("organization_id", "id", name="uq_remediation_runs_org_id"),
        sa.CheckConstraint(
            "issues_considered_count >= 0", name="ck_remediation_runs_considered_nonneg"
        ),
        sa.CheckConstraint(
            "total_changes_count >= 0", name="ck_remediation_runs_changes_nonneg"
        ),
        sa.CheckConstraint(
            "issues_skipped_count >= 0", name="ck_remediation_runs_skipped_nonneg"
        ),
        sa.CheckConstraint(
            "total_changes_count + issues_skipped_count = issues_considered_count",
            name="ck_remediation_runs_counts_reconcile",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_remediation_runs_organization_id_organizations", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_remediation_runs_org_task_run", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_remediation_runs_org_task", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_remediation_runs_org_data_source", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "source_task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_remediation_runs_org_source_task_run", ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_remediation_runs_organization_id", "remediation_runs", ["organization_id"]
    )
    op.create_index(
        "ix_remediation_runs_task_run_id", "remediation_runs", ["task_run_id"], unique=True
    )
    op.create_index("ix_remediation_runs_task_id", "remediation_runs", ["task_id"])
    op.create_index(
        "ix_remediation_runs_data_source_id", "remediation_runs", ["data_source_id"]
    )
    op.create_index(
        "ix_remediation_runs_source_task_run_id", "remediation_runs", ["source_task_run_id"]
    )

    # =========================================================================
    # remediation_changes: append-only per-proposal record. source_issue_id
    # is a REQUIRED composite FK into issues (organization_id, id) --
    # every RemediationChange traces to exactly one Module 14 Issue (decision
    # 6). action is a plain, CHECK-validated string -- same "small, internal,
    # worker/config-owned value set -> plain string" precedent every closed
    # vocabulary in this project uses. confidence is CHECK-pinned to 1.0
    # (rule-based only, per the Module 15 requirements contract).
    # =========================================================================
    actions_sql = ", ".join(f"'{a}'" for a in (
        "trim_whitespace",
        "collapse_multiple_spaces",
        "standardize_capitalization",
        "normalize_boolean",
        "normalize_date",
        "normalize_phone",
        "normalize_numeric",
        "normalize_enum_value",
        "remove_duplicate_row",
        "remove_duplicate_primary_key",
    ))
    op.create_table(
        "remediation_changes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("remediation_run_id", sa.Uuid(), nullable=False),
        sa.Column("source_issue_id", sa.Uuid(), nullable=False),
        sa.Column("row_number", sa.Integer(), nullable=False),
        sa.Column("column_name", sa.String(length=255), nullable=True),
        sa.Column("action", sa.String(length=30), nullable=False),
        sa.Column("original_value", sa.Text(), nullable=True),
        sa.Column("proposed_value", sa.Text(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_remediation_changes"),
        sa.CheckConstraint(f"action IN ({actions_sql})", name="ck_remediation_changes_action_valid"),
        sa.CheckConstraint("row_number >= 0", name="ck_remediation_changes_row_number_nonneg"),
        sa.CheckConstraint("confidence = 1.0", name="ck_remediation_changes_confidence_fixed"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_remediation_changes_organization_id_organizations", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "remediation_run_id"],
            ["remediation_runs.organization_id", "remediation_runs.id"],
            name="fk_remediation_changes_org_remediation_run", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "source_issue_id"],
            ["issues.organization_id", "issues.id"],
            name="fk_remediation_changes_org_source_issue", ondelete="RESTRICT",
        ),
    )
    op.create_index(
        "ix_remediation_changes_organization_id", "remediation_changes", ["organization_id"]
    )
    op.create_index(
        "ix_remediation_changes_remediation_run_id",
        "remediation_changes", ["remediation_run_id"],
    )
    op.create_index(
        "ix_remediation_changes_source_issue_id", "remediation_changes", ["source_issue_id"]
    )
    op.create_index("ix_remediation_changes_action", "remediation_changes", ["action"])

    # =========================================================================
    # remediation_column_rules: organization-configured, per-column
    # governance for every remediation action that must never guess.
    # Structural sibling of issue_detection_column_rules (Module 14) /
    # standardization_column_mappings (Module 7) -- same two-partial-
    # unique-index pattern (NULL != NULL under standard SQL uniqueness
    # semantics).
    # =========================================================================
    capitalization_targets_sql = "'lower', 'upper', 'title'"
    op.create_table(
        "remediation_column_rules",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=True),
        sa.Column("column_name", sa.String(length=255), nullable=False),
        sa.Column("source_date_format", sa.String(length=50), nullable=True),
        sa.Column("target_date_format", sa.String(length=50), nullable=True),
        sa.Column("default_country", sa.String(length=2), nullable=True),
        sa.Column("capitalization_target", sa.String(length=10), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_remediation_column_rules"),
        sa.CheckConstraint(
            f"capitalization_target IS NULL OR capitalization_target IN ({capitalization_targets_sql})",
            name="ck_remediation_column_rules_capitalization_target_valid",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_remediation_column_rules_org_id", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_remediation_column_rules_org_data_source", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"],
            name="fk_remediation_column_rules_created_by_users", ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_remediation_column_rules_organization_id",
        "remediation_column_rules", ["organization_id"],
    )
    op.create_index(
        "ix_remediation_column_rules_scoped_active",
        "remediation_column_rules",
        ["organization_id", "data_source_id", sa.text("lower(trim(column_name))")],
        unique=True,
        postgresql_where=sa.text("is_active = true AND data_source_id IS NOT NULL"),
        sqlite_where=sa.text("is_active = 1 AND data_source_id IS NOT NULL"),
    )
    op.create_index(
        "ix_remediation_column_rules_orgwide_active",
        "remediation_column_rules",
        ["organization_id", sa.text("lower(trim(column_name))")],
        unique=True,
        postgresql_where=sa.text("is_active = true AND data_source_id IS NULL"),
        sqlite_where=sa.text("is_active = 1 AND data_source_id IS NULL"),
    )

    # =========================================================================
    # remediation_dataset_configs: organization-configured, per-data-source
    # toggle for the two whole-row removal proposals. Deliberately a
    # SEPARATE table from remediation_column_rules -- duplicate removal is a
    # whole-dataset decision, not a per-column one. Unlike
    # remediation_column_rules, data_source_id here is REQUIRED (no org-wide
    # fallback) -- removal is consequential enough that it must be opted
    # into per data source explicitly.
    # =========================================================================
    op.create_table(
        "remediation_dataset_configs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=False),
        sa.Column(
            "remove_duplicate_rows_enabled", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
        sa.Column(
            "remove_duplicate_primary_keys_enabled", sa.Boolean(), nullable=False,
            server_default=sa.false(),
        ),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_remediation_dataset_configs"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_remediation_dataset_configs_org_id", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_remediation_dataset_configs_org_data_source", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"],
            name="fk_remediation_dataset_configs_created_by_users", ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_remediation_dataset_configs_organization_id",
        "remediation_dataset_configs", ["organization_id"],
    )
    op.create_index(
        "ix_remediation_dataset_configs_data_source_id",
        "remediation_dataset_configs", ["data_source_id"],
    )
    op.create_index(
        "ix_remediation_dataset_configs_active",
        "remediation_dataset_configs",
        ["organization_id", "data_source_id"],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
        sqlite_where=sa.text("is_active = 1"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_remediation_dataset_configs_active", table_name="remediation_dataset_configs"
    )
    op.drop_index(
        "ix_remediation_dataset_configs_data_source_id",
        table_name="remediation_dataset_configs",
    )
    op.drop_index(
        "ix_remediation_dataset_configs_organization_id",
        table_name="remediation_dataset_configs",
    )
    op.drop_table("remediation_dataset_configs")

    op.drop_index(
        "ix_remediation_column_rules_orgwide_active", table_name="remediation_column_rules"
    )
    op.drop_index(
        "ix_remediation_column_rules_scoped_active", table_name="remediation_column_rules"
    )
    op.drop_index(
        "ix_remediation_column_rules_organization_id", table_name="remediation_column_rules"
    )
    op.drop_table("remediation_column_rules")

    op.drop_index("ix_remediation_changes_action", table_name="remediation_changes")
    op.drop_index(
        "ix_remediation_changes_source_issue_id", table_name="remediation_changes"
    )
    op.drop_index(
        "ix_remediation_changes_remediation_run_id", table_name="remediation_changes"
    )
    op.drop_index(
        "ix_remediation_changes_organization_id", table_name="remediation_changes"
    )
    op.drop_table("remediation_changes")

    op.drop_index("ix_remediation_runs_source_task_run_id", table_name="remediation_runs")
    op.drop_index("ix_remediation_runs_data_source_id", table_name="remediation_runs")
    op.drop_index("ix_remediation_runs_task_id", table_name="remediation_runs")
    op.drop_index("ix_remediation_runs_task_run_id", table_name="remediation_runs")
    op.drop_index("ix_remediation_runs_organization_id", table_name="remediation_runs")
    op.drop_table("remediation_runs")

    # Module 14 additive touch 2 reversal.
    with op.batch_alter_table("issues") as batch_op:
        batch_op.drop_constraint("uq_issues_org_id", type_="unique")

    # Module 14 additive touch 1 reversal.
    op.drop_column("issue_detection_runs", "source_sha256")

    # task_type_enum: PostgreSQL has no DROP VALUE for enum types. Same
    # documented, accepted limitation as every prior enum-extension
    # migration in this project. Does not affect downgrade->upgrade cycle
    # correctness: no row will contain 'remediate' after a genuine downgrade
    # in a fresh verification database, since remediation_runs (the only
    # table that would ever reference it indirectly) is dropped above, and
    # tasks.task_type is a plain string column, not itself checked against
    # this specific value by any CHECK constraint added in this migration.
