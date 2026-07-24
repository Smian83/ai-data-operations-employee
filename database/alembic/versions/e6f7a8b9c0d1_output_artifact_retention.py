"""output artifact retention

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-07-23 15:00:00.000000

Module 13 -- Output Artifact Retention & Lifecycle Management. Purely
additive: three nullable output_deleted_at columns (cleaning_runs,
standardization_runs, export_runs), one new append-only audit table
(artifact_retention_events, a structural mirror of artifact_download_events
from Module 10's b4c5d6e7f8a9), and a widened outcome vocabulary on
artifact_download_events (adds 'purged', so a download attempt against a
retention-purged artifact is distinguishable from an unexpected
file_missing). No enum type, no TaskType change, no worker-table change.

Lifecycle note (see docs/module-13-output-artifact-retention-design.md):
this migration stores only the durable facts of the four-state artifact
lifecycle (ACTIVE/EXPIRED/PURGE_PENDING/PURGED) -- ACTIVE and EXPIRED are
derived at query time from each run's existing status/decision-timestamp
columns and are never stored; PURGE_PENDING is a transaction-scoped row
lock, never stored on the run row itself (see app.worker.retention and
artifact_retention_event.py's own docstrings -- 'started' remains a valid
outcome value in the CHECK constraint below for schema forward-
compatibility but is not written by app.worker.retention today, which
inserts each row exactly once, already at its terminal outcome, inside
the same transaction as the claim); PURGED is the one genuinely new
durable fact, captured by output_deleted_at. No new "lifecycle_state"
column is added anywhere, by design.

No backfill: every existing run row gets NULL for output_deleted_at, which
is exactly and only "this artifact has not been purged" -- correct for
every row that exists today, since nothing has ever deleted an output file
before this module. CSV_INPUT_ROOT (the tenant's own source data) is never
touched by anything in this migration or this module.

SQLite caveat, handled explicitly here following the exact precedent
d5e6f7a8b9c0_scheduled_task_execution.py established: widening
artifact_download_events' two outcome-related CHECK constraints requires
op.batch_alter_table on SQLite (which cannot ALTER a constraint in place).
Unlike ix_tasks_org_name_active (an expression-based index that
batch_alter_table's reflect-and-copy step could not preserve),
ix_artifact_download_events_outcome is a plain single-column index, which
SQLAlchemy's reflection handles correctly -- confirmed empirically against
a real, Alembic-migrated SQLite database while authoring this migration
(the full test suite's own migration-cycle verification, run via pytest
against this migration, is the durable record of that confirmation, not a
dedicated migration-only test file). It is still explicitly dropped and
recreated around the batch_alter_table block anyway, as a defensive,
zero-cost measure consistent with this project's "evidence over
assumption" standard, not because reflection was found to fail here.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6f7a8b9c0d1"
down_revision: Union[str, None] = "d5e6f7a8b9c0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD_OUTCOME_VALUES = ("started", "completed", "integrity_failed", "file_missing", "stream_failed")
_NEW_OUTCOME_VALUES = _OLD_OUTCOME_VALUES + ("purged",)


def _drop_outcome_index() -> None:
    op.drop_index("ix_artifact_download_events_outcome", table_name="artifact_download_events")


def _create_outcome_index() -> None:
    op.create_index(
        "ix_artifact_download_events_outcome",
        "artifact_download_events",
        ["outcome"],
    )


def upgrade() -> None:
    # =========================================================================
    # cleaning_runs / standardization_runs / export_runs: one additive,
    # nullable output_deleted_at column each. NULL means "artifact still
    # present"; non-NULL is the sole authoritative "this file no longer
    # exists" signal (Module 13's PURGED state).
    # =========================================================================
    op.add_column(
        "cleaning_runs",
        sa.Column("output_deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "standardization_runs",
        sa.Column("output_deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "export_runs",
        sa.Column("output_deleted_at", sa.DateTime(timezone=True), nullable=True),
    )

    # =========================================================================
    # artifact_retention_events: one row per retention-pass evaluation of a
    # CleaningRun/StandardizationRun/ExportRun's output artifact. Exactly
    # one of cleaning_run_id/standardization_run_id/export_run_id is set
    # per row, same shape as artifact_download_events. Each row is written
    # exactly once, already at its terminal outcome (completed/
    # already_missing/failed), inside the same transaction as the claim --
    # 'started' remains a valid outcome value for schema forward-
    # compatibility but is not produced today (see app.worker.retention).
    # =========================================================================
    op.create_table(
        "artifact_retention_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("cleaning_run_id", sa.Uuid(), nullable=True),
        sa.Column("standardization_run_id", sa.Uuid(), nullable=True),
        sa.Column("export_run_id", sa.Uuid(), nullable=True),
        sa.Column(
            "outcome", sa.String(length=20), nullable=False, server_default="started"
        ),
        sa.Column("failure_reason_code", sa.String(length=50), nullable=True),
        sa.Column(
            "dry_run", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
        sa.Column("retention_window_days_applied", sa.Integer(), nullable=False),
        sa.Column("artifact_size_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_artifact_retention_events"),
        sa.CheckConstraint(
            "(CASE WHEN cleaning_run_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN standardization_run_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN export_run_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_artifact_retention_events_exactly_one_run_ref",
        ),
        sa.CheckConstraint(
            "outcome IN ('started', 'completed', 'already_missing', 'failed')",
            name="ck_artifact_retention_events_outcome_valid",
        ),
        # A failure_reason_code exists exactly when outcome='failed'. Unlike
        # artifact_download_events, 'already_missing' is NOT a failure here
        # -- it is an expected, non-error convergence outcome (the file was
        # already gone, so the PURGED end state is reached anyway).
        sa.CheckConstraint(
            "(outcome IN ('started', 'completed', 'already_missing') "
            "AND failure_reason_code IS NULL) OR "
            "(outcome = 'failed' AND failure_reason_code IS NOT NULL)",
            name="ck_artifact_retention_events_failure_reason_matches_outcome",
        ),
        # Small, controlled vocabulary -- see
        # ARTIFACT_RETENTION_FAILURE_REASON_CODES in
        # app/models/artifact_retention_event.py for the full rationale
        # per code. Never a raw exception message, never a filesystem
        # path.
        sa.CheckConstraint(
            "failure_reason_code IS NULL OR failure_reason_code IN "
            "('unsafe_path', 'permission_denied', 'invalid_artifact_type', "
            "'filesystem_error', 'database_conflict')",
            name="ck_artifact_retention_events_failure_reason_code_valid",
        ),
        sa.CheckConstraint(
            "(outcome = 'started' AND completed_at IS NULL) OR "
            "(outcome != 'started' AND completed_at IS NOT NULL)",
            name="ck_artifact_retention_events_completed_at_matches_outcome",
        ),
        sa.CheckConstraint(
            "retention_window_days_applied >= 0",
            name="ck_artifact_retention_events_window_days_nonnegative",
        ),
        # Database-layer backstop mirroring Module 12's
        # ck_tasks_schedule_interval_hard_floor precedent: independent of
        # app.core.config.Settings' own configurable, fail-fast-at-startup
        # minimum (RETENTION_WINDOW_HARD_FLOOR_DAYS = 7, deliberately
        # hand-kept-in-sync, not derived -- a CHECK constraint cannot read
        # an environment variable). This does not stop a bad deletion by
        # itself; it stops an audit row from ever asserting an
        # out-of-policy window was applied, exactly the same defense-in-
        # depth reasoning as the schedule-interval floor.
        sa.CheckConstraint(
            "retention_window_days_applied >= 7",
            name="ck_artifact_retention_events_window_days_hard_floor",
        ),
        sa.CheckConstraint(
            "artifact_size_bytes IS NULL OR artifact_size_bytes >= 0",
            name="ck_artifact_retention_events_artifact_size_bytes_nonneg",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_artifact_retention_events_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "cleaning_run_id"],
            ["cleaning_runs.organization_id", "cleaning_runs.id"],
            name="fk_artifact_retention_events_org_cleaning_run", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "standardization_run_id"],
            ["standardization_runs.organization_id", "standardization_runs.id"],
            name="fk_artifact_retention_events_org_stdz_run", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "export_run_id"],
            ["export_runs.organization_id", "export_runs.id"],
            name="fk_artifact_retention_events_org_export_run", ondelete="CASCADE",
        ),
    )
    op.create_index(
        "ix_artifact_retention_events_organization_id",
        "artifact_retention_events", ["organization_id"],
    )
    op.create_index(
        "ix_artifact_retention_events_cleaning_run_id",
        "artifact_retention_events", ["cleaning_run_id"],
    )
    op.create_index(
        "ix_artifact_retention_events_standardization_run_id",
        "artifact_retention_events", ["standardization_run_id"],
    )
    op.create_index(
        "ix_artifact_retention_events_export_run_id",
        "artifact_retention_events", ["export_run_id"],
    )
    op.create_index(
        "ix_artifact_retention_events_outcome",
        "artifact_retention_events", ["outcome"],
    )

    # =========================================================================
    # artifact_download_events: widen outcome to add 'purged', and adjust
    # the failure-reason-matches-outcome constraint so 'purged' (like
    # 'started'/'completed') never requires a failure_reason_code --
    # 'purged' is an expected, intentional state, not a failure.
    # =========================================================================
    _drop_outcome_index()
    with op.batch_alter_table("artifact_download_events") as batch_op:
        batch_op.drop_constraint(
            "ck_artifact_download_events_failure_reason_matches_outcome", type_="check"
        )
        batch_op.drop_constraint("ck_artifact_download_events_outcome_valid", type_="check")
        batch_op.create_check_constraint(
            "ck_artifact_download_events_outcome_valid",
            "outcome IN (" + ", ".join(f"'{o}'" for o in _NEW_OUTCOME_VALUES) + ")",
        )
        batch_op.create_check_constraint(
            "ck_artifact_download_events_failure_reason_matches_outcome",
            "(outcome IN ('started', 'completed', 'purged') "
            "AND failure_reason_code IS NULL) OR "
            "(outcome IN ('integrity_failed', 'file_missing', 'stream_failed') "
            "AND failure_reason_code IS NOT NULL)",
        )
    _create_outcome_index()


def downgrade() -> None:
    _drop_outcome_index()
    with op.batch_alter_table("artifact_download_events") as batch_op:
        batch_op.drop_constraint(
            "ck_artifact_download_events_failure_reason_matches_outcome", type_="check"
        )
        batch_op.drop_constraint("ck_artifact_download_events_outcome_valid", type_="check")
        batch_op.create_check_constraint(
            "ck_artifact_download_events_outcome_valid",
            "outcome IN (" + ", ".join(f"'{o}'" for o in _OLD_OUTCOME_VALUES) + ")",
        )
        batch_op.create_check_constraint(
            "ck_artifact_download_events_failure_reason_matches_outcome",
            "(outcome IN ('started', 'completed') AND failure_reason_code IS NULL) OR "
            "(outcome NOT IN ('started', 'completed') AND failure_reason_code IS NOT NULL)",
        )
    _create_outcome_index()

    op.drop_index(
        "ix_artifact_retention_events_outcome", table_name="artifact_retention_events"
    )
    op.drop_index(
        "ix_artifact_retention_events_export_run_id", table_name="artifact_retention_events"
    )
    op.drop_index(
        "ix_artifact_retention_events_standardization_run_id",
        table_name="artifact_retention_events",
    )
    op.drop_index(
        "ix_artifact_retention_events_cleaning_run_id", table_name="artifact_retention_events"
    )
    op.drop_index(
        "ix_artifact_retention_events_organization_id", table_name="artifact_retention_events"
    )
    op.drop_table("artifact_retention_events")

    op.drop_column("export_runs", "output_deleted_at")
    op.drop_column("standardization_runs", "output_deleted_at")
    op.drop_column("cleaning_runs", "output_deleted_at")
