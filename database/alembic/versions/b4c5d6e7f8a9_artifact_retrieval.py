"""artifact retrieval

Revision ID: b4c5d6e7f8a9
Revises: a2b3c4d5e6f7
Create Date: 2026-07-21 20:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b4c5d6e7f8a9"
down_revision: Union[str, None] = "a2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # =========================================================================
    # No ALTER TYPE step in this migration -- Module 10 adds no TaskType
    # value and no worker changes; it is purely additive API + one new
    # audit table serving bytes CleaningHandler/StandardizationHandler/
    # ExportHandler already wrote. See
    # docs/module-10-artifact-retrieval-design.md Section 7.
    #
    # artifact_download_events: one row per AUTHORIZED download attempt
    # against a CleaningRun/StandardizationRun/ExportRun output file.
    # Exactly one of cleaning_run_id/standardization_run_id/export_run_id
    # is set per row. outcome starts at 'started' and transitions exactly
    # once to a terminal value (completed/integrity_failed/file_missing/
    # stream_failed).
    # =========================================================================
    op.create_table(
        "artifact_download_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_type", sa.String(length=20), nullable=False),
        sa.Column("cleaning_run_id", sa.Uuid(), nullable=True),
        sa.Column("standardization_run_id", sa.Uuid(), nullable=True),
        sa.Column("export_run_id", sa.Uuid(), nullable=True),
        sa.Column("downloaded_by", sa.Uuid(), nullable=True),
        sa.Column("run_status_at_request", sa.String(length=20), nullable=False),
        sa.Column(
            "outcome", sa.String(length=20), nullable=False, server_default="started"
        ),
        sa.Column("failure_reason_code", sa.String(length=50), nullable=True),
        sa.Column("verified_sha256", sa.String(length=64), nullable=True),
        sa.Column(
            "bytes_served", sa.BigInteger(), nullable=False, server_default="0"
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True),
            server_default=sa.func.now(), nullable=False,
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name="pk_artifact_download_events"),
        sa.CheckConstraint(
            "artifact_type IN ('cleaning', 'standardization', 'export')",
            name="ck_artifact_download_events_artifact_type_valid",
        ),
        sa.CheckConstraint(
            "(CASE WHEN cleaning_run_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN standardization_run_id IS NOT NULL THEN 1 ELSE 0 END) + "
            "(CASE WHEN export_run_id IS NOT NULL THEN 1 ELSE 0 END) = 1",
            name="ck_artifact_download_events_exactly_one_run_ref",
        ),
        sa.CheckConstraint(
            "run_status_at_request IN ('approved', 'rolled_back')",
            name="ck_artifact_download_events_run_status_valid",
        ),
        sa.CheckConstraint(
            "outcome IN ('started', 'completed', 'integrity_failed', "
            "'file_missing', 'stream_failed')",
            name="ck_artifact_download_events_outcome_valid",
        ),
        sa.CheckConstraint(
            "(outcome IN ('started', 'completed') AND failure_reason_code IS NULL) OR "
            "(outcome NOT IN ('started', 'completed') AND failure_reason_code IS NOT NULL)",
            name="ck_artifact_download_events_failure_reason_matches_outcome",
        ),
        sa.CheckConstraint(
            "failure_reason_code IS NULL OR failure_reason_code IN ("
            "'hash_mismatch', 'file_not_found', 'not_a_regular_file', "
            "'path_containment_violation', 'io_error', 'stream_interrupted')",
            name="ck_artifact_download_events_failure_reason_code_valid",
        ),
        sa.CheckConstraint(
            "(outcome = 'started' AND completed_at IS NULL) OR "
            "(outcome != 'started' AND completed_at IS NOT NULL)",
            name="ck_artifact_download_events_completed_at_matches_outcome",
        ),
        sa.CheckConstraint(
            "bytes_served >= 0",
            name="ck_artifact_download_events_bytes_served_nonnegative",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id"], ["organizations.id"],
            name="fk_artifact_download_events_organization_id_organizations",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "cleaning_run_id"],
            ["cleaning_runs.organization_id", "cleaning_runs.id"],
            name="fk_artifact_download_events_org_cleaning_run", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "standardization_run_id"],
            ["standardization_runs.organization_id", "standardization_runs.id"],
            name="fk_artifact_download_events_org_standardization_run", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["organization_id", "export_run_id"],
            ["export_runs.organization_id", "export_runs.id"],
            name="fk_artifact_download_events_org_export_run", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["downloaded_by"], ["users.id"],
            name="fk_artifact_download_events_downloaded_by_users", ondelete="SET NULL",
        ),
    )
    op.create_index(
        "ix_artifact_download_events_organization_id",
        "artifact_download_events", ["organization_id"],
    )
    op.create_index(
        "ix_artifact_download_events_cleaning_run_id",
        "artifact_download_events", ["cleaning_run_id"],
    )
    op.create_index(
        "ix_artifact_download_events_standardization_run_id",
        "artifact_download_events", ["standardization_run_id"],
    )
    op.create_index(
        "ix_artifact_download_events_export_run_id",
        "artifact_download_events", ["export_run_id"],
    )
    op.create_index(
        "ix_artifact_download_events_downloaded_by",
        "artifact_download_events", ["downloaded_by"],
    )
    op.create_index(
        "ix_artifact_download_events_outcome",
        "artifact_download_events", ["outcome"],
    )


def downgrade() -> None:
    op.drop_index("ix_artifact_download_events_outcome", table_name="artifact_download_events")
    op.drop_index(
        "ix_artifact_download_events_downloaded_by", table_name="artifact_download_events"
    )
    op.drop_index(
        "ix_artifact_download_events_export_run_id", table_name="artifact_download_events"
    )
    op.drop_index(
        "ix_artifact_download_events_standardization_run_id",
        table_name="artifact_download_events",
    )
    op.drop_index(
        "ix_artifact_download_events_cleaning_run_id", table_name="artifact_download_events"
    )
    op.drop_index(
        "ix_artifact_download_events_organization_id", table_name="artifact_download_events"
    )
    op.drop_table("artifact_download_events")
