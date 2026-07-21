"""data standardization engine

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-07-20 23:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6a7b8c9"
down_revision: Union[str, None] = "c3d4e5f6a7b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # =========================================================================
    # task_type_enum: add 'standardize' to the existing native PostgreSQL
    # enum type. This is the project's first ALTER TYPE ... ADD VALUE
    # (every prior enum-touching migration created a brand-new type
    # instead). PostgreSQL (12+, this project targets 16) allows ADD VALUE
    # inside a transaction, but the new label cannot be USED within that
    # same transaction -- it must be committed first. Alembic wraps each
    # migration in a transaction by default, so this statement must run in
    # its own autocommit block, per Alembic's documented pattern for
    # exactly this PostgreSQL restriction.
    #
    # SQLite has no native enum type and needs no equivalent change here:
    # verified directly against a real Alembic-migrated (not
    # Base.metadata.create_all()-built) SQLite database before writing
    # this migration -- tasks.task_type there is a plain VARCHAR(9) NOT
    # NULL with no CHECK constraint at all (the migration-local enum
    # objects in this project's prior revisions never set
    # create_constraint=True; only the ORM model's copy does, and Alembic
    # migrations never consult the ORM model). SQLite also does not
    # enforce declared VARCHAR lengths, so the longer value fits without
    # any column-width change either.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute("ALTER TYPE task_type_enum ADD VALUE IF NOT EXISTS 'standardize'")

    # =========================================================================
    # standardization_runs: one row per Module 7 standardization TaskRun.
    # Direct structural mirror of cleaning_runs, including the
    # UniqueConstraint(organization_id, id) Module 6's equivalent table
    # initially shipped without (see c3d4e5f6a7b8's own commit history) --
    # added here from the start.
    # =========================================================================
    op.create_table(
        "standardization_runs",
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
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column("standardization_engine_version", sa.String(length=20), nullable=False),
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
        sa.PrimaryKeyConstraint("id", name="pk_standardization_runs"),
        sa.UniqueConstraint("task_run_id", name="uq_standardization_runs_task_run_id"),
        sa.UniqueConstraint("organization_id", "id", name="uq_standardization_runs_org_id"),
        sa.CheckConstraint(
            "status IN ('pending_review', 'approved', 'rejected', 'rolled_back')",
            name="ck_standardization_runs_status_valid",
        ),
        sa.CheckConstraint("row_count >= 0", name="ck_standardization_runs_row_count_nonnegative"),
        sa.CheckConstraint(
            "total_changes_count >= 0", name="ck_standardization_runs_total_changes_nonnegative"
        ),
        sa.CheckConstraint(
            "confidence_score >= 0 AND confidence_score <= 1",
            name="ck_standardization_runs_confidence_range",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_standardization_runs_organization_id_organizations", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_standardization_runs_org_task_run", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_standardization_runs_org_task", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_standardization_runs_org_data_source", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "source_task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_standardization_runs_org_source_task_run", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"], ["users.id"],
            name="fk_standardization_runs_approved_by_users", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["rejected_by"], ["users.id"],
            name="fk_standardization_runs_rejected_by_users", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["rolled_back_by"], ["users.id"],
            name="fk_standardization_runs_rolled_back_by_users", ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_standardization_runs_organization_id", "standardization_runs", ["organization_id"]
    )
    op.create_index(
        "ix_standardization_runs_task_run_id", "standardization_runs", ["task_run_id"],
        unique=True,
    )
    op.create_index("ix_standardization_runs_task_id", "standardization_runs", ["task_id"])
    op.create_index(
        "ix_standardization_runs_data_source_id", "standardization_runs", ["data_source_id"]
    )
    op.create_index(
        "ix_standardization_runs_source_task_run_id",
        "standardization_runs", ["source_task_run_id"],
    )

    # =========================================================================
    # standardization_changes: append-only per-cell change log, capped per
    # run at settings.standardization_max_persisted_changes.
    # =========================================================================
    op.create_table(
        "standardization_changes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("standardization_run_id", sa.Uuid(), nullable=False),
        sa.Column("row_index", sa.Integer(), nullable=False),
        sa.Column("column_name", sa.String(length=255), nullable=False),
        sa.Column("field_type", sa.String(length=30), nullable=False),
        sa.Column("original_value", sa.Text(), nullable=False),
        sa.Column("standardized_value", sa.Text(), nullable=False),
        sa.Column("rule_name", sa.String(length=50), nullable=False),
        sa.Column("rule_version", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("confidence_score", sa.Float(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_standardization_changes"),
        sa.CheckConstraint(
            "row_index >= 0", name="ck_standardization_changes_row_index_nonnegative"
        ),
        sa.CheckConstraint(
            "confidence_score >= 0 AND confidence_score <= 1",
            name="ck_standardization_changes_confidence_range",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "standardization_run_id"],
            ["standardization_runs.organization_id", "standardization_runs.id"],
            name="fk_standardization_changes_org_standardization_run", ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_standardization_changes_organization_id",
        "standardization_changes", ["organization_id"],
    )
    op.create_index(
        "ix_standardization_changes_standardization_run_id",
        "standardization_changes", ["standardization_run_id"],
    )

    # =========================================================================
    # standardization_column_mappings: organization-configured column ->
    # field_type overrides. Two partial unique indexes (not one) because
    # data_source_id is nullable ("applies org-wide") and NULL != NULL
    # under standard SQL uniqueness semantics -- see the model's docstring.
    # =========================================================================
    field_types_sql = (
        "'person_name', 'company_name', 'email', 'phone', 'postal_address', "
        "'city', 'state_province', 'country', 'postal_code', 'date', 'time', "
        "'boolean', 'numeric', 'currency'"
    )
    op.create_table(
        "standardization_column_mappings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=True),
        sa.Column("column_name", sa.String(length=255), nullable=False),
        sa.Column("field_type", sa.String(length=30), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_standardization_column_mappings"),
        sa.CheckConstraint(
            f"field_type IN ({field_types_sql})",
            name="ck_standardization_column_mappings_field_type_valid",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_standardization_column_mappings_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_standardization_column_mappings_org_data_source", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"],
            name="fk_standardization_column_mappings_created_by_users", ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_standardization_column_mappings_organization_id",
        "standardization_column_mappings", ["organization_id"],
    )
    op.create_index(
        "ix_standardization_column_mappings_scoped_active",
        "standardization_column_mappings",
        ["organization_id", "data_source_id", sa.text("lower(trim(column_name))")],
        unique=True,
        postgresql_where=sa.text("is_active = true AND data_source_id IS NOT NULL"),
        sqlite_where=sa.text("is_active = 1 AND data_source_id IS NOT NULL"),
    )
    op.create_index(
        "ix_standardization_column_mappings_orgwide_active",
        "standardization_column_mappings",
        ["organization_id", sa.text("lower(trim(column_name))")],
        unique=True,
        postgresql_where=sa.text("is_active = true AND data_source_id IS NULL"),
        sqlite_where=sa.text("is_active = 1 AND data_source_id IS NULL"),
    )

    # =========================================================================
    # standardization_lookup_entries: organization-supplied lookup-table
    # entries. Same two-partial-index reasoning as column_mappings above,
    # applied to the nullable field_type column instead of data_source_id.
    # =========================================================================
    op.create_table(
        "standardization_lookup_entries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("field_type", sa.String(length=30), nullable=True),
        sa.Column("lookup_key", sa.String(length=255), nullable=False),
        sa.Column("lookup_value", sa.String(length=255), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_standardization_lookup_entries"),
        sa.CheckConstraint(
            f"field_type IS NULL OR field_type IN ({field_types_sql})",
            name="ck_standardization_lookup_entries_field_type_valid",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_standardization_lookup_entries_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"],
            name="fk_standardization_lookup_entries_created_by_users", ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_standardization_lookup_entries_organization_id",
        "standardization_lookup_entries", ["organization_id"],
    )
    op.create_index(
        "ix_standardization_lookup_entries_scoped_active",
        "standardization_lookup_entries",
        ["organization_id", "field_type", sa.text("lower(trim(lookup_key))")],
        unique=True,
        postgresql_where=sa.text("is_active = true AND field_type IS NOT NULL"),
        sqlite_where=sa.text("is_active = 1 AND field_type IS NOT NULL"),
    )
    op.create_index(
        "ix_standardization_lookup_entries_global_active",
        "standardization_lookup_entries",
        ["organization_id", sa.text("lower(trim(lookup_key))")],
        unique=True,
        postgresql_where=sa.text("is_active = true AND field_type IS NULL"),
        sqlite_where=sa.text("is_active = 1 AND field_type IS NULL"),
    )


def downgrade() -> None:
    op.drop_index(
        "ix_standardization_lookup_entries_global_active",
        table_name="standardization_lookup_entries",
    )
    op.drop_index(
        "ix_standardization_lookup_entries_scoped_active",
        table_name="standardization_lookup_entries",
    )
    op.drop_index(
        "ix_standardization_lookup_entries_organization_id",
        table_name="standardization_lookup_entries",
    )
    op.drop_table("standardization_lookup_entries")

    op.drop_index(
        "ix_standardization_column_mappings_orgwide_active",
        table_name="standardization_column_mappings",
    )
    op.drop_index(
        "ix_standardization_column_mappings_scoped_active",
        table_name="standardization_column_mappings",
    )
    op.drop_index(
        "ix_standardization_column_mappings_organization_id",
        table_name="standardization_column_mappings",
    )
    op.drop_table("standardization_column_mappings")

    op.drop_index(
        "ix_standardization_changes_standardization_run_id",
        table_name="standardization_changes",
    )
    op.drop_index(
        "ix_standardization_changes_organization_id", table_name="standardization_changes"
    )
    op.drop_table("standardization_changes")

    op.drop_index("ix_standardization_runs_source_task_run_id", table_name="standardization_runs")
    op.drop_index("ix_standardization_runs_data_source_id", table_name="standardization_runs")
    op.drop_index("ix_standardization_runs_task_id", table_name="standardization_runs")
    op.drop_index("ix_standardization_runs_task_run_id", table_name="standardization_runs")
    op.drop_index("ix_standardization_runs_organization_id", table_name="standardization_runs")
    op.drop_table("standardization_runs")

    # task_type_enum: PostgreSQL has no DROP VALUE for enum types. Removing
    # 'standardize' would require rebuilding the entire type and every
    # column/table that references it -- out of proportion to what a
    # downgrade needs to accomplish, and the standard, documented approach
    # for this exact situation is to leave the label in place. This does
    # not affect downgrade->upgrade cycle correctness: no row will contain
    # 'standardize' after a genuine downgrade in a fresh verification
    # database, since standardization_runs (the only table that would ever
    # reference it indirectly) is dropped above, and tasks.task_type is a
    # plain string column, not itself checked against this specific value
    # by any CHECK constraint added in this migration.
