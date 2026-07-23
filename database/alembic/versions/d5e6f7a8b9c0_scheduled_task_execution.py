"""scheduled task execution

Revision ID: d5e6f7a8b9c0
Revises: c5d6e7f8a9b0
Create Date: 2026-07-22 14:00:00.000000

Module 12 -- Scheduled Task Execution. Adds two additive, nullable columns
to `tasks` (schedule_interval_seconds, next_run_at), two CHECK constraints,
and one partial index. No table, no enum value, and no change to any
existing column. `tasks.schedule` (the pre-existing free-text column) is
untouched -- it is deprecated as of this module (see
docs/module-12-scheduled-task-execution-design.md Section 5) but neither
its type nor its data is migrated, reinterpreted, or read by anything this
migration adds.

No backfill: every existing row gets NULL/NULL for the two new columns,
which is exactly and only "not scheduled" -- identical to how every task
in the system has always behaved, since nothing has ever read
schedule_interval_seconds before this module.

ck_tasks_schedule_interval_hard_floor (>= 30 seconds) is a fixed,
non-configurable database safety floor -- deliberately independent of
app.core.config.Settings.minimum_schedule_interval_seconds (the real,
operator-facing, configurable minimum, default 60s, enforced in Pydantic
on every write path). See app/core/config.py's SCHEDULE_INTERVAL_HARD_
FLOOR_SECONDS constant for why these two "30"s are a deliberately
hand-kept-in-sync pair rather than a single derived source of truth: a
CHECK constraint cannot read an environment variable at row-write time.

IMPORTANT SQLite caveat, discovered and handled explicitly here: SQLite
cannot ADD/DROP a table CHECK constraint in place, so op.batch_alter_table
recreates the whole `tasks` table under the hood on that dialect (exactly
as a1c2d4f6b8e0's ck_task_runs_lease_consistency already required). That
table-copy step reflects the table first, and Alembic/SQLAlchemy cannot
reflect ix_tasks_org_name_active (an expression-based partial index --
`lower(trim(name))` filtered `WHERE is_active`) -- confirmed directly
against a real SQLite database while authoring this migration: without
explicit handling, ix_tasks_org_name_active is silently DROPPED and never
recreated by the table-copy, which would silently remove the tenant-scoped
active-task-name-uniqueness guarantee. This migration therefore drops that
index explicitly before, and recreates it explicitly after, each
batch_alter_table block -- unconditionally, on both dialects (a harmless,
no-op-equivalent extra DROP/CREATE pair on PostgreSQL, where
batch_alter_table never touches the index at all).
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e6f7a8b9c0"
down_revision: Union[str, None] = "c5d6e7f8a9b0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _drop_org_name_active_index() -> None:
    op.drop_index("ix_tasks_org_name_active", table_name="tasks")


def _create_org_name_active_index() -> None:
    # Identical definition to e8e9044941dd_create_data_sources_tasks_and_
    # task_runs.py's original op.create_index call -- restored verbatim,
    # not redesigned.
    op.create_index(
        "ix_tasks_org_name_active",
        "tasks",
        ["organization_id", sa.text("lower(trim(name))")],
        unique=True,
        postgresql_where=sa.text("is_active = true"),
        sqlite_where=sa.text("is_active = 1"),
    )


def upgrade() -> None:
    op.add_column("tasks", sa.Column("schedule_interval_seconds", sa.Integer(), nullable=True))
    op.add_column(
        "tasks",
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
    )
    # batch_alter_table: on PostgreSQL this compiles to plain ALTER TABLE ADD
    # CONSTRAINT statements; SQLite cannot ADD CONSTRAINT to an existing
    # table at all, so Alembic transparently recreates the table under the
    # hood. Same precedent as ck_task_runs_lease_consistency in
    # a1c2d4f6b8e0_task_execution_engine.py -- required so this migration
    # runs identically on both dialects. See the module docstring for why
    # ix_tasks_org_name_active is explicitly dropped/recreated around it.
    _drop_org_name_active_index()
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.create_check_constraint(
            "ck_tasks_schedule_interval_hard_floor",
            "schedule_interval_seconds IS NULL OR schedule_interval_seconds >= 30",
        )
        batch_op.create_check_constraint(
            "ck_tasks_schedule_consistency",
            "(schedule_interval_seconds IS NULL AND next_run_at IS NULL)"
            " OR (schedule_interval_seconds IS NOT NULL AND next_run_at IS NOT NULL)",
        )
    _create_org_name_active_index()
    op.create_index(
        "ix_tasks_scheduled_due",
        "tasks",
        ["next_run_at", "id"],
        postgresql_where=sa.text("schedule_interval_seconds IS NOT NULL AND is_active = true"),
        sqlite_where=sa.text("schedule_interval_seconds IS NOT NULL AND is_active = 1"),
    )


def downgrade() -> None:
    op.drop_index("ix_tasks_scheduled_due", table_name="tasks")
    _drop_org_name_active_index()
    with op.batch_alter_table("tasks") as batch_op:
        batch_op.drop_constraint("ck_tasks_schedule_consistency", type_="check")
        batch_op.drop_constraint("ck_tasks_schedule_interval_hard_floor", type_="check")
    _create_org_name_active_index()
    op.drop_column("tasks", "next_run_at")
    op.drop_column("tasks", "schedule_interval_seconds")
