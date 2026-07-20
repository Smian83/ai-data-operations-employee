"""create data sources tasks and task runs

Revision ID: e8e9044941dd
Revises: b3e2e4e74b4b
Create Date: 2026-07-20 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e8e9044941dd"
down_revision: Union[str, None] = "b3e2e4e74b4b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _create_enum_idempotent(bind, enum_type: sa.Enum) -> None:
    """Create a native PostgreSQL enum type without raising if it already
    exists.

    sa.Enum.create(bind, checkfirst=True) is *supposed* to guarantee this,
    but its existence check queries the database at the moment this
    function runs -- it says nothing about whether alembic_version
    correctly reflects that state. If this migration's upgrade() is ever
    re-entered against a database that already has these types (e.g.
    alembic_version was reset, restored from an older snapshot, or points
    at an earlier revision than the database's actual objects reflect --
    exactly what happened during Module 4's real-PostgreSQL verification),
    checkfirst=True does not help, because from Alembic's point of view
    this migration has "not run yet" and it dutifully re-executes upgrade()
    from the top.

    A PostgreSQL-native guarded DDL block (CREATE TYPE wrapped in a DO
    block that swallows duplicate_object) sidesteps that entirely: it is
    safe to execute unconditionally, regardless of what alembic_version
    claims. On non-PostgreSQL dialects (the SQLite sandbox), native enum
    types do not exist at all -- create_constraint=True on the model
    already expresses "enum-ness" as a per-column CHECK constraint instead,
    so Enum.create() there is already a safe no-op and needs no change.
    """
    if bind.dialect.name != "postgresql":
        enum_type.create(bind, checkfirst=True)
        return
    values_sql = ", ".join(f"'{value}'" for value in enum_type.enums)
    bind.execute(
        sa.text(
            f"""
            DO $$
            BEGIN
                CREATE TYPE {enum_type.name} AS ENUM ({values_sql});
            EXCEPTION
                WHEN duplicate_object THEN NULL;
            END
            $$;
            """
        )
    )


def _drop_enum_idempotent(bind, enum_type: sa.Enum) -> None:
    """Symmetric counterpart to _create_enum_idempotent -- DROP TYPE IF
    EXISTS is unconditionally safe to re-run on PostgreSQL, regardless of
    alembic_version state."""
    if bind.dialect.name != "postgresql":
        enum_type.drop(bind, checkfirst=True)
        return
    bind.execute(sa.text(f"DROP TYPE IF EXISTS {enum_type.name}"))

# create_type=False: we create/drop these explicitly in upgrade()/downgrade()
# rather than letting create_table() auto-manage them, which is the
# recommended pattern for enums shared across upgrade/downgrade cycles.
# IMPORTANT: these must be sqlalchemy.dialects.postgresql.ENUM, NOT the
# generic sa.Enum. The generic sa.Enum silently drops an unrecognized
# create_type kwarg (its __init__ only ever pops native_enum,
# create_constraint, values_callable, sort_key_function, length,
# omit_aliases, validate_strings -- never create_type), so create_type=False
# passed to sa.Enum has *no effect at all*. At CREATE TABLE compile time,
# SQLAlchemy adapts a generic Enum into a native postgresql.ENUM via
# ENUM.adapt_emulated_to_native(), which only forwards create_type from the
# original object when that object was ALREADY a NativeForEmulated type
# (i.e. already postgresql.ENUM) -- a plain sa.Enum never qualifies, so the
# adapted object falls back to the constructor default of create_type=True,
# and op.create_table() below silently emits its own CREATE TYPE regardless
# of what was passed here. This was the actual root cause of the
# DuplicateObject failure during Module 4 PostgreSQL verification: the type
# created by _create_enum_idempotent() below was immediately re-created (and
# collided) by op.create_table()'s own automatic enum DDL. Only
# postgresql.ENUM genuinely implements and honors create_type.
source_type_enum = postgresql.ENUM(
    "postgres", "mysql", "rest_api", "csv_upload", "s3", "other",
    name="source_type_enum",
    create_type=False,
)
task_type_enum = postgresql.ENUM(
    "sync", "transform", "export", "other",
    name="task_type_enum",
    create_type=False,
)
task_run_status_enum = postgresql.ENUM(
    "pending", "running", "success", "failed",
    name="task_run_status_enum",
    create_type=False,
)


def upgrade() -> None:
    bind = op.get_bind()
    _create_enum_idempotent(bind, source_type_enum)
    _create_enum_idempotent(bind, task_type_enum)
    _create_enum_idempotent(bind, task_run_status_enum)

    # --- data_sources ---------------------------------------------------
    op.create_table(
        "data_sources",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("source_type", source_type_enum, nullable=False),
        sa.Column("connection_metadata", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_data_sources"),
        # Required so `tasks` can have a tenant-aware composite FK pointing
        # at (organization_id, id) — see fk_tasks_org_data_source below.
        sa.UniqueConstraint("organization_id", "id", name="uq_data_sources_org_id"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_data_sources_organization_id_organizations", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"],
            name="fk_data_sources_created_by_users", ondelete="SET NULL",
        ),
    )
    op.create_index("ix_data_sources_organization_id", "data_sources", ["organization_id"])
    # Case-insensitive, whitespace-trimmed uniqueness among ACTIVE rows only
    # — soft-deleting a data source frees its name for reuse.
    op.create_index(
        "ix_data_sources_org_name_active",
        "data_sources",
        ["organization_id", sa.text("lower(trim(name))")],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
        sqlite_where=sa.text("is_active = 1"),
    )

    # --- tasks ------------------------------------------------------------
    op.create_table(
        "tasks",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("task_type", task_type_enum, nullable=False),
        sa.Column("schedule", sa.String(length=100), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_tasks"),
        sa.UniqueConstraint("organization_id", "id", name="uq_tasks_org_id"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_tasks_organization_id_organizations", ondelete="CASCADE",
        ),
        # Tenant-aware composite FK: data_source_id, when set, MUST belong
        # to a DataSource in the SAME organization_id. NULL data_source_id
        # satisfies the constraint automatically (standard multi-column FK
        # semantics) — a task with no source is allowed.
        sa.ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_tasks_org_data_source", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["created_by"], ["users.id"],
            name="fk_tasks_created_by_users", ondelete="SET NULL",
        ),
    )
    op.create_index("ix_tasks_organization_id", "tasks", ["organization_id"])
    op.create_index("ix_tasks_data_source_id", "tasks", ["data_source_id"])
    op.create_index(
        "ix_tasks_org_name_active",
        "tasks",
        ["organization_id", sa.text("lower(trim(name))")],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
        sqlite_where=sa.text("is_active = 1"),
    )

    # --- task_runs ----------------------------------------------------------
    op.create_table(
        "task_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        # Denormalized (also derivable via task_id -> tasks.organization_id)
        # so tenant-scoped queries on this — the highest-volume table —
        # never depend on remembering to join through Task, and so the
        # tenant-aware composite FK below is possible at all.
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status", task_run_status_enum, nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column("triggered_by", sa.Uuid(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("log_output", sa.Text(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_runs"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_task_runs_organization_id_organizations", ondelete="CASCADE",
        ),
        # Tenant-aware composite FK: organization_id must match the
        # referenced Task's organization_id. Enforced by the database.
        sa.ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_task_runs_org_task", ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["triggered_by"], ["users.id"],
            name="fk_task_runs_triggered_by_users", ondelete="SET NULL",
        ),
        sa.CheckConstraint(
            "(status = 'pending' AND started_at IS NULL AND finished_at IS NULL"
            "  AND error_message IS NULL)"
            " OR (status = 'running' AND started_at IS NOT NULL AND finished_at IS NULL)"
            " OR (status = 'success' AND started_at IS NOT NULL AND finished_at IS NOT NULL"
            "     AND error_message IS NULL)"
            " OR (status = 'failed' AND started_at IS NOT NULL AND finished_at IS NOT NULL"
            "     AND error_message IS NOT NULL)",
            name="ck_task_runs_status_invariants",
        ),
        sa.CheckConstraint(
            "finished_at IS NULL OR started_at IS NULL OR finished_at >= started_at",
            name="ck_task_runs_finished_after_started",
        ),
    )
    op.create_index("ix_task_runs_organization_id", "task_runs", ["organization_id"])
    op.create_index("ix_task_runs_task_id", "task_runs", ["task_id"])


def downgrade() -> None:
    op.drop_index("ix_task_runs_task_id", table_name="task_runs")
    op.drop_index("ix_task_runs_organization_id", table_name="task_runs")
    op.drop_table("task_runs")

    op.drop_index("ix_tasks_org_name_active", table_name="tasks")
    op.drop_index("ix_tasks_data_source_id", table_name="tasks")
    op.drop_index("ix_tasks_organization_id", table_name="tasks")
    op.drop_table("tasks")

    op.drop_index("ix_data_sources_org_name_active", table_name="data_sources")
    op.drop_index("ix_data_sources_organization_id", table_name="data_sources")
    op.drop_table("data_sources")

    bind = op.get_bind()
    _drop_enum_idempotent(bind, task_run_status_enum)
    _drop_enum_idempotent(bind, task_type_enum)
    _drop_enum_idempotent(bind, source_type_enum)
