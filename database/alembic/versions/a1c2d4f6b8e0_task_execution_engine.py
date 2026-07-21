"""task execution engine (leases, retries, idempotency, audit events, credentials)

Revision ID: a1c2d4f6b8e0
Revises: e8e9044941dd
Create Date: 2026-07-20 12:00:00.000000

"""
import uuid
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c2d4f6b8e0"
down_revision: Union[str, None] = "e8e9044941dd"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()

    # =========================================================================
    # task_runs: additive execution-engine columns. Module 3's CHECK
    # constraints (ck_task_runs_status_invariants, ck_task_runs_finished_
    # after_started) are untouched -- a retry-driven requeue resets
    # started_at/finished_at/error_message back to NULL, so those existing
    # constraints already cover the new 'running -> pending' path with zero
    # changes.
    # =========================================================================
    op.add_column("task_runs", sa.Column("idempotency_key", sa.Uuid(), nullable=True))
    op.add_column("task_runs", sa.Column("lease_token", sa.Uuid(), nullable=True))
    op.add_column(
        "task_runs", sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "task_runs", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "task_runs",
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "task_runs", sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True)
    )

    # Backfill idempotency_key for any pre-existing rows before enforcing
    # NOT NULL + UNIQUE. Hand-rolled per-row UPDATE (not a database-specific
    # random-UUID function) so this runs identically on PostgreSQL and the
    # SQLite sandbox. Module 3 shipped very recently and this table is not
    # yet carrying production traffic, so a straightforward loop is
    # appropriate at this scale.
    task_runs_tbl = sa.table(
        "task_runs", sa.column("id", sa.Uuid()), sa.column("idempotency_key", sa.Uuid())
    )
    existing_ids = [row[0] for row in bind.execute(sa.select(task_runs_tbl.c.id)).fetchall()]
    for row_id in existing_ids:
        bind.execute(
            task_runs_tbl.update()
            .where(task_runs_tbl.c.id == row_id)
            .values(idempotency_key=uuid.uuid4())
        )

    # batch_alter_table: on PostgreSQL this compiles to plain ALTER TABLE
    # statements; on SQLite (which cannot ALTER COLUMN or ADD/DROP
    # CONSTRAINT in place at all) Alembic transparently recreates the table.
    # Using it here means this migration runs identically on both, which is
    # required since SQLite is our sandbox verification target.
    with op.batch_alter_table("task_runs") as batch_op:
        batch_op.alter_column("idempotency_key", nullable=False)
        batch_op.create_unique_constraint(
            "uq_task_runs_idempotency_key", ["idempotency_key"]
        )
        # Required so TaskRunEvent can have a tenant-aware composite FK
        # (organization_id, task_run_id) -> (organization_id, id), same
        # pattern as every other Module 3/4 parent table.
        batch_op.create_unique_constraint("uq_task_runs_org_id", ["organization_id", "id"])
        batch_op.create_check_constraint(
            "ck_task_runs_lease_consistency",
            "(status = 'running' AND lease_token IS NOT NULL AND lease_expires_at IS NOT NULL)"
            " OR (status != 'running' AND lease_token IS NULL AND lease_expires_at IS NULL)",
        )

    # =========================================================================
    # tasks: additive per-task execution-engine overrides. NULL means "use
    # the worker's global default" -- see app.core.config.
    # =========================================================================
    op.add_column("tasks", sa.Column("max_attempts", sa.Integer(), nullable=True))
    op.add_column("tasks", sa.Column("timeout_seconds", sa.Integer(), nullable=True))

    # =========================================================================
    # task_run_events: append-only execution audit trail.
    # =========================================================================
    op.create_table(
        "task_run_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("task_run_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("from_status", sa.String(length=20), nullable=True),
        sa.Column("to_status", sa.String(length=20), nullable=True),
        sa.Column("worker_id", sa.String(length=255), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_task_run_events"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_task_run_events_organization_id_organizations", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_task_run_events_org_task_run", ondelete="CASCADE",
        ),
    )
    op.create_index("ix_task_run_events_organization_id", "task_run_events", ["organization_id"])
    op.create_index("ix_task_run_events_task_run_id", "task_run_events", ["task_run_id"])

    # =========================================================================
    # data_source_credentials: encrypted-at-rest live credentials, one row
    # per DataSource. See app.worker.credentials for the CredentialProvider
    # abstraction that wraps this table -- nothing else queries it directly.
    # =========================================================================
    op.create_table(
        "data_source_credentials",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=False),
        sa.Column("encrypted_payload", sa.LargeBinary(), nullable=False),
        sa.Column("key_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.PrimaryKeyConstraint("id", name="pk_data_source_credentials"),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_data_source_credentials_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_data_source_credentials_org_data_source", ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_data_source_credentials_organization_id", "data_source_credentials", ["organization_id"]
    )
    # This single unique index is the ENTIRE uniqueness contract for
    # data_source_id -- deliberately not also declared as a table-level
    # UniqueConstraint. That would create a second, redundant unique object
    # covering the identical column and diverge from the model's actual DDL
    # contract: DataSourceCredential.data_source_id is declared with
    # `unique=True, index=True` (no separate `unique=True`-only column),
    # which per SQLAlchemy's Column semantics compiles to exactly one
    # unique Index -- never a UniqueConstraint -- confirmed by compiling
    # CreateTable(DataSourceCredential.__table__) against the postgresql
    # dialect directly. An earlier draft of this migration additionally
    # declared `sa.UniqueConstraint(..., name="uq_data_source_credentials_
    # data_source_id")` here, which created a second unique-enforcing
    # object the model never asked for -- the same class of migration/model
    # DDL-ownership drift as the Module 3 enum bug, just on a constraint
    # instead of a type. Removed; do not re-add without also adding the
    # matching UniqueConstraint (and a corresponding drop) to the model.
    op.create_index(
        "ix_data_source_credentials_data_source_id", "data_source_credentials", ["data_source_id"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_data_source_credentials_data_source_id", table_name="data_source_credentials")
    op.drop_index("ix_data_source_credentials_organization_id", table_name="data_source_credentials")
    op.drop_table("data_source_credentials")

    op.drop_index("ix_task_run_events_task_run_id", table_name="task_run_events")
    op.drop_index("ix_task_run_events_organization_id", table_name="task_run_events")
    op.drop_table("task_run_events")

    op.drop_column("tasks", "timeout_seconds")
    op.drop_column("tasks", "max_attempts")

    with op.batch_alter_table("task_runs") as batch_op:
        batch_op.drop_constraint("ck_task_runs_lease_consistency", type_="check")
        batch_op.drop_constraint("uq_task_runs_org_id", type_="unique")
        batch_op.drop_constraint("uq_task_runs_idempotency_key", type_="unique")
        batch_op.drop_column("next_retry_at")
        batch_op.drop_column("attempt_count")
        batch_op.drop_column("last_heartbeat_at")
        batch_op.drop_column("lease_expires_at")
        batch_op.drop_column("lease_token")
        batch_op.drop_column("idempotency_key")
