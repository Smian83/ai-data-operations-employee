"""data matching and deduplication

Revision ID: f1a2b3c4d5e6
Revises: d4e5f6a7b8c9
Create Date: 2026-07-21 12:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "d4e5f6a7b8c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # =========================================================================
    # task_type_enum: add 'match' to the existing native PostgreSQL enum
    # type. Second ALTER TYPE ... ADD VALUE in this project (Module 7's
    # 'standardize' addition was the first) -- same proven, now-repeated
    # pattern: the new label cannot be used within the same transaction it
    # is added in, so this runs in its own autocommit block.
    #
    # SQLite needs no equivalent change -- tasks.task_type is a plain
    # VARCHAR with no CHECK constraint on a real Alembic-migrated SQLite
    # database (re-confirmed against Module 7's own finding), and SQLite
    # does not enforce declared VARCHAR lengths either.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE task_type_enum ADD VALUE IF NOT EXISTS 'match'")

    # =========================================================================
    # match_rule_sets: organization-configured, versioned matching
    # configuration. Created BEFORE match_runs because match_runs.
    # rule_set_id has a composite FK into this table. Two partial unique
    # indexes for the same reason standardization_column_mappings needs
    # two: data_source_id is nullable ("applies org-wide") and NULL !=
    # NULL under standard SQL uniqueness semantics.
    # =========================================================================
    op.create_table(
        "match_rule_sets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("duplicate_threshold", sa.Float(), nullable=False),
        sa.Column("review_threshold", sa.Float(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_match_rule_sets"),
        sa.UniqueConstraint("organization_id", "id", name="uq_match_rule_sets_org_id"),
        sa.CheckConstraint("version >= 1", name="ck_match_rule_sets_version_min"),
        sa.CheckConstraint(
            "review_threshold >= 0 AND review_threshold < duplicate_threshold "
            "AND duplicate_threshold <= 1",
            name="ck_match_rule_sets_threshold_order",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_match_rule_sets_organization_id_organizations", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_match_rule_sets_org_data_source", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"],
            name="fk_match_rule_sets_created_by_users", ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_match_rule_sets_organization_id", "match_rule_sets", ["organization_id"]
    )
    op.create_index(
        "ix_match_rule_sets_scoped_active",
        "match_rule_sets",
        ["organization_id", "data_source_id"],
        unique=True,
        postgresql_where=sa.text("is_active = true AND data_source_id IS NOT NULL"),
        sqlite_where=sa.text("is_active = 1 AND data_source_id IS NOT NULL"),
    )
    op.create_index(
        "ix_match_rule_sets_orgwide_active",
        "match_rule_sets",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text("is_active = true AND data_source_id IS NULL"),
        sqlite_where=sa.text("is_active = 1 AND data_source_id IS NULL"),
    )

    # =========================================================================
    # match_rule_fields: the weighted field list for one match_rule_set.
    # =========================================================================
    op.create_table(
        "match_rule_fields",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("rule_set_id", sa.Uuid(), nullable=False),
        sa.Column("column_name", sa.String(length=255), nullable=False),
        sa.Column("comparison_type", sa.String(length=20), nullable=False),
        sa.Column("weight", sa.Float(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_match_rule_fields"),
        sa.UniqueConstraint(
            "rule_set_id", "column_name", name="uq_match_rule_fields_set_column"
        ),
        sa.CheckConstraint(
            "comparison_type IN ('exact', 'normalized_exact')",
            name="ck_match_rule_fields_comparison_type_valid",
        ),
        sa.CheckConstraint("weight > 0", name="ck_match_rule_fields_weight_positive"),
        sa.ForeignKeyConstraint(
            ["organization_id", "rule_set_id"],
            ["match_rule_sets.organization_id", "match_rule_sets.id"],
            name="fk_match_rule_fields_org_rule_set", ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_match_rule_fields_organization_id", "match_rule_fields", ["organization_id"]
    )
    op.create_index(
        "ix_match_rule_fields_rule_set_id", "match_rule_fields", ["rule_set_id"]
    )

    # =========================================================================
    # match_runs: one row per Module 8 MATCH TaskRun. Direct structural
    # mirror of standardization_runs' idempotency/approval-state shape,
    # minus output_file_path/output_sha256 (Module 8 produces no output
    # file -- see the design doc Section 2), plus rule_set_id/
    # rule_set_version and the matching-specific aggregate counters.
    # =========================================================================
    op.create_table(
        "match_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("task_run_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=False),
        sa.Column("source_task_run_id", sa.Uuid(), nullable=False),
        sa.Column("rule_set_id", sa.Uuid(), nullable=True),
        sa.Column("rule_set_version", sa.Integer(), nullable=True),
        sa.Column("row_count", sa.Integer(), nullable=False),
        sa.Column("total_comparisons_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_group_count", sa.Integer(), nullable=False),
        sa.Column("duplicate_pairs_count", sa.Integer(), nullable=False),
        sa.Column("ambiguous_pairs_count", sa.Integer(), nullable=False),
        sa.Column("skipped_block_count", sa.Integer(), nullable=False),
        sa.Column("decisions_by_rule", sa.JSON(), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("match_engine_version", sa.String(length=20), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_match_runs"),
        sa.UniqueConstraint("task_run_id", name="uq_match_runs_task_run_id"),
        sa.UniqueConstraint("organization_id", "id", name="uq_match_runs_org_id"),
        sa.CheckConstraint(
            "status IN ('pending_review', 'approved', 'rejected', 'rolled_back')",
            name="ck_match_runs_status_valid",
        ),
        sa.CheckConstraint("row_count >= 0", name="ck_match_runs_row_count_nonnegative"),
        sa.CheckConstraint(
            "total_comparisons_count >= 0", name="ck_match_runs_total_comparisons_nonnegative"
        ),
        sa.CheckConstraint(
            "duplicate_group_count >= 0", name="ck_match_runs_duplicate_group_count_nonnegative"
        ),
        sa.CheckConstraint(
            "duplicate_pairs_count >= 0", name="ck_match_runs_duplicate_pairs_nonnegative"
        ),
        sa.CheckConstraint(
            "ambiguous_pairs_count >= 0", name="ck_match_runs_ambiguous_pairs_nonnegative"
        ),
        sa.CheckConstraint(
            "skipped_block_count >= 0", name="ck_match_runs_skipped_block_count_nonnegative"
        ),
        sa.CheckConstraint(
            "confidence_score >= 0 AND confidence_score <= 1",
            name="ck_match_runs_confidence_range",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_match_runs_organization_id_organizations", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_match_runs_org_task_run", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_match_runs_org_task", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_match_runs_org_data_source", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "source_task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_match_runs_org_source_task_run", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "rule_set_id"],
            ["match_rule_sets.organization_id", "match_rule_sets.id"],
            name="fk_match_runs_org_rule_set", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"], ["users.id"],
            name="fk_match_runs_approved_by_users", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["rejected_by"], ["users.id"],
            name="fk_match_runs_rejected_by_users", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["rolled_back_by"], ["users.id"],
            name="fk_match_runs_rolled_back_by_users", ondelete="SET NULL",
        ),
    )
    op.create_index("ix_match_runs_organization_id", "match_runs", ["organization_id"])
    op.create_index("ix_match_runs_task_run_id", "match_runs", ["task_run_id"], unique=True)
    op.create_index("ix_match_runs_task_id", "match_runs", ["task_id"])
    op.create_index("ix_match_runs_data_source_id", "match_runs", ["data_source_id"])
    op.create_index(
        "ix_match_runs_source_task_run_id", "match_runs", ["source_task_run_id"]
    )
    op.create_index("ix_match_runs_rule_set_id", "match_runs", ["rule_set_id"])

    # =========================================================================
    # match_groups: one row per duplicate cluster.
    # =========================================================================
    op.create_table(
        "match_groups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("match_run_id", sa.Uuid(), nullable=False),
        sa.Column("canonical_row_index", sa.Integer(), nullable=False),
        sa.Column("record_count", sa.Integer(), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_match_groups"),
        sa.UniqueConstraint("organization_id", "id", name="uq_match_groups_org_id"),
        sa.CheckConstraint("record_count >= 2", name="ck_match_groups_record_count_min"),
        sa.CheckConstraint(
            "confidence_score >= 0 AND confidence_score <= 1",
            name="ck_match_groups_confidence_range",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "match_run_id"],
            ["match_runs.organization_id", "match_runs.id"],
            name="fk_match_groups_org_match_run", ondelete="CASCADE",
        ),
    )
    op.create_index("ix_match_groups_organization_id", "match_groups", ["organization_id"])
    op.create_index("ix_match_groups_match_run_id", "match_groups", ["match_run_id"])

    # =========================================================================
    # match_decisions: one row per pairwise comparison that reached at
    # least the review threshold, capped per run at
    # settings.match_max_persisted_decisions. blocking_key (approved
    # design revision) is nullable -- NULL for Stage-1 (exact_row_match)
    # decisions, always set for Stage-2 decisions.
    # =========================================================================
    op.create_table(
        "match_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("match_run_id", sa.Uuid(), nullable=False),
        sa.Column("match_group_id", sa.Uuid(), nullable=True),
        sa.Column("record_a_row_index", sa.Integer(), nullable=False),
        sa.Column("record_b_row_index", sa.Integer(), nullable=False),
        sa.Column("blocking_key", sa.String(length=500), nullable=True),
        sa.Column("rule_name", sa.String(length=50), nullable=False),
        sa.Column("field_comparisons", sa.JSON(), nullable=False),
        sa.Column("total_score", sa.Float(), nullable=False),
        sa.Column("threshold_used", sa.Float(), nullable=False),
        sa.Column("decision", sa.String(length=20), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("rule_version", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_match_decisions"),
        sa.CheckConstraint(
            "record_a_row_index < record_b_row_index", name="ck_match_decisions_row_order"
        ),
        sa.CheckConstraint(
            "total_score >= 0 AND total_score <= 1", name="ck_match_decisions_total_score_range"
        ),
        sa.CheckConstraint(
            "threshold_used >= 0 AND threshold_used <= 1",
            name="ck_match_decisions_threshold_range",
        ),
        sa.CheckConstraint(
            "decision IN ('duplicate', 'ambiguous')", name="ck_match_decisions_decision_valid"
        ),
        sa.CheckConstraint(
            "confidence_score >= 0 AND confidence_score <= 1",
            name="ck_match_decisions_confidence_range",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "match_run_id"],
            ["match_runs.organization_id", "match_runs.id"],
            name="fk_match_decisions_org_match_run", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "match_group_id"],
            ["match_groups.organization_id", "match_groups.id"],
            name="fk_match_decisions_org_match_group", ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_match_decisions_organization_id", "match_decisions", ["organization_id"]
    )
    op.create_index("ix_match_decisions_match_run_id", "match_decisions", ["match_run_id"])
    op.create_index(
        "ix_match_decisions_match_group_id", "match_decisions", ["match_group_id"]
    )
    op.create_index(
        "ix_match_decisions_run_blocking_key",
        "match_decisions", ["match_run_id", "blocking_key"],
    )

    # =========================================================================
    # match_skipped_blocks (new in the approved design revision): one row
    # per block skipped for exceeding settings.match_max_block_size.
    # =========================================================================
    op.create_table(
        "match_skipped_blocks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("match_run_id", sa.Uuid(), nullable=False),
        sa.Column("blocking_key", sa.String(length=500), nullable=False),
        sa.Column("block_size", sa.Integer(), nullable=False),
        sa.Column("sample_row_indices", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_match_skipped_blocks"),
        sa.UniqueConstraint(
            "match_run_id", "blocking_key", name="uq_match_skipped_blocks_run_key"
        ),
        sa.CheckConstraint("block_size > 0", name="ck_match_skipped_blocks_size_positive"),
        sa.ForeignKeyConstraint(
            ["organization_id", "match_run_id"],
            ["match_runs.organization_id", "match_runs.id"],
            name="fk_match_skipped_blocks_org_run", ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_match_skipped_blocks_organization_id", "match_skipped_blocks", ["organization_id"]
    )
    op.create_index(
        "ix_match_skipped_blocks_match_run_id", "match_skipped_blocks", ["match_run_id"]
    )


def downgrade() -> None:
    op.drop_index(
        "ix_match_skipped_blocks_match_run_id", table_name="match_skipped_blocks"
    )
    op.drop_index(
        "ix_match_skipped_blocks_organization_id", table_name="match_skipped_blocks"
    )
    op.drop_table("match_skipped_blocks")

    op.drop_index("ix_match_decisions_run_blocking_key", table_name="match_decisions")
    op.drop_index("ix_match_decisions_match_group_id", table_name="match_decisions")
    op.drop_index("ix_match_decisions_match_run_id", table_name="match_decisions")
    op.drop_index("ix_match_decisions_organization_id", table_name="match_decisions")
    op.drop_table("match_decisions")

    op.drop_index("ix_match_groups_match_run_id", table_name="match_groups")
    op.drop_index("ix_match_groups_organization_id", table_name="match_groups")
    op.drop_table("match_groups")

    op.drop_index("ix_match_runs_rule_set_id", table_name="match_runs")
    op.drop_index("ix_match_runs_source_task_run_id", table_name="match_runs")
    op.drop_index("ix_match_runs_data_source_id", table_name="match_runs")
    op.drop_index("ix_match_runs_task_id", table_name="match_runs")
    op.drop_index("ix_match_runs_task_run_id", table_name="match_runs")
    op.drop_index("ix_match_runs_organization_id", table_name="match_runs")
    op.drop_table("match_runs")

    op.drop_index("ix_match_rule_fields_rule_set_id", table_name="match_rule_fields")
    op.drop_index("ix_match_rule_fields_organization_id", table_name="match_rule_fields")
    op.drop_table("match_rule_fields")

    op.drop_index("ix_match_rule_sets_orgwide_active", table_name="match_rule_sets")
    op.drop_index("ix_match_rule_sets_scoped_active", table_name="match_rule_sets")
    op.drop_index("ix_match_rule_sets_organization_id", table_name="match_rule_sets")
    op.drop_table("match_rule_sets")

    # task_type_enum: PostgreSQL has no DROP VALUE for enum types -- same
    # documented, standard limitation as Module 7's 'standardize' addition.
    # The label is left in place on downgrade; this does not affect
    # downgrade->upgrade cycle correctness in a fresh verification
    # database, since every table that could reference it is dropped
    # above and tasks.task_type is a plain string column with no CHECK
    # constraint added by this migration.
