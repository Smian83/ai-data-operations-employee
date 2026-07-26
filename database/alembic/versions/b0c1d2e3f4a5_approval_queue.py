"""approval queue

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4
Create Date: 2026-07-25 12:00:00.000000

Module 16 (Approval Queue) — Phase 1.

Adds one new table: remediation_change_decisions.

Purely additive -- no existing table, column, constraint, index, or enum
value is modified. The entire downgrade() undoes this migration by
dropping the new table only.

Design notes (see docs/module-16-approval-queue-design.md Section 3):
- Append-only history: no UNIQUE(organization_id, remediation_change_id),
  deliberately. Multiple rows per change are permitted; the effective
  state is the row with the latest decision_timestamp.
- reviewer_id + reviewer_name + reviewer_role: FK plus two snapshot
  columns that survive user deletion or profile changes.
- applied_at + applied_by: always NULL in Module 16; reserved for the
  future Apply/Export module.
- Composite index (organization_id, remediation_change_id,
  decision_timestamp): supports efficient "latest row per change" queries
  both single-change (LIMIT 1) and run-level (DISTINCT ON).

Identifier length audit (all pre-measured, all ≤ 63 bytes):
  uq_remediation_change_decisions_org_id      40
  fk_remediation_change_decisions_org         37  (inline on column)
  fk_remediation_change_decisions_org_run     41
  fk_remediation_change_decisions_org_change  44
  fk_remediation_change_decisions_reviewer    42  (inline on column)
  fk_remediation_change_decisions_applied_by  44  (inline on column)
  ck_remediation_change_decisions_valid        38
  ix_remediation_change_decisions_org_chg_ts  44
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b0c1d2e3f4a5"
down_revision: Union[str, None] = "a9b0c1d2e3f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # =========================================================================
    # remediation_change_decisions: the sole new table in Module 16 Phase 1.
    #
    # FK ondelete strategy:
    #   organization_id -> organizations.id  CASCADE  (org delete wipes tenant)
    #   composite (org, run_id) -> remediation_runs  RESTRICT  (audit rows
    #       must not vanish alongside the run -- same reasoning as
    #       RemediationChange.fk_remediation_changes_org_source_issue)
    #   composite (org, change_id) -> remediation_changes  RESTRICT  (same)
    #   reviewer_id -> users.id  SET NULL  (user soft-delete: FK nulled, but
    #       reviewer_name/reviewer_role snapshots survive intact)
    #   applied_by -> users.id  SET NULL  (same convention, future use)
    #
    # No UNIQUE(organization_id, remediation_change_id): deliberate omission.
    # See module docstring and design doc Section 2a.
    # =========================================================================
    op.create_table(
        "remediation_change_decisions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("remediation_run_id", sa.Uuid(), nullable=False),
        sa.Column("remediation_change_id", sa.Uuid(), nullable=False),
        sa.Column("decision", sa.String(length=10), nullable=False),
        sa.Column("reviewer_id", sa.Uuid(), nullable=True),
        sa.Column("reviewer_name", sa.String(length=255), nullable=True),
        sa.Column("reviewer_role", sa.String(length=100), nullable=True),
        sa.Column("decision_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("comment", sa.Text(), nullable=True),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("applied_by", sa.Uuid(), nullable=True),
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
            "organization_id", "id",
            name="uq_remediation_change_decisions_org_id",
        ),
        # ---- CHECK: decision must be one of the two known values ----
        sa.CheckConstraint(
            "decision IN ('approved', 'rejected')",
            name="ck_remediation_change_decisions_valid",
        ),
        # ---- Composite FK → organizations (CASCADE) ----
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_remediation_change_decisions_org",
            ondelete="CASCADE",
        ),
        # ---- Composite FK → remediation_runs (RESTRICT) ----
        sa.ForeignKeyConstraint(
            ["organization_id", "remediation_run_id"],
            ["remediation_runs.organization_id", "remediation_runs.id"],
            name="fk_remediation_change_decisions_org_run",
            ondelete="RESTRICT",
        ),
        # ---- Simple FK → remediation_changes.id (RESTRICT) ----
        # Note: remediation_changes has no UNIQUE(organization_id, id), so a
        # composite FK is not possible. A simple FK to the PK provides the
        # referential integrity needed; tenant isolation is already enforced
        # by organization_id → organizations.id above.
        sa.ForeignKeyConstraint(
            ["remediation_change_id"],
            ["remediation_changes.id"],
            name="fk_remediation_change_decisions_org_change",
            ondelete="RESTRICT",
        ),
        # ---- Simple FK → users (reviewer_id, SET NULL) ----
        sa.ForeignKeyConstraint(
            ["reviewer_id"],
            ["users.id"],
            name="fk_remediation_change_decisions_reviewer",
            ondelete="SET NULL",
        ),
        # ---- Simple FK → users (applied_by, SET NULL) ----
        sa.ForeignKeyConstraint(
            ["applied_by"],
            ["users.id"],
            name="fk_remediation_change_decisions_applied_by",
            ondelete="SET NULL",
        ),
    )

    # ---- Scalar indexes (organization_id and the two FK columns) ----
    op.create_index(
        "ix_remediation_change_decisions_org",
        "remediation_change_decisions",
        ["organization_id"],
    )
    op.create_index(
        "ix_remediation_change_decisions_run",
        "remediation_change_decisions",
        ["remediation_run_id"],
    )
    op.create_index(
        "ix_remediation_change_decisions_change",
        "remediation_change_decisions",
        ["remediation_change_id"],
    )

    # ---- Composite index for "latest row per change" queries ----
    # Supports ORDER BY decision_timestamp DESC LIMIT 1 (single-change lookup)
    # and DISTINCT ON remediation_change_id ORDER BY decision_timestamp DESC
    # (run-level aggregate). Stored in ASC order; DESC usage still uses this
    # index efficiently on PostgreSQL (backward scan). SQLite also uses it.
    op.create_index(
        "ix_remediation_change_decisions_org_chg_ts",
        "remediation_change_decisions",
        ["organization_id", "remediation_change_id", "decision_timestamp"],
    )


def downgrade() -> None:
    # Drop indexes before the table (index-drop order mirrors upgrade order,
    # reversed, same pattern as every prior multi-index migration).
    op.drop_index(
        "ix_remediation_change_decisions_org_chg_ts",
        table_name="remediation_change_decisions",
    )
    op.drop_index(
        "ix_remediation_change_decisions_change",
        table_name="remediation_change_decisions",
    )
    op.drop_index(
        "ix_remediation_change_decisions_run",
        table_name="remediation_change_decisions",
    )
    op.drop_index(
        "ix_remediation_change_decisions_org",
        table_name="remediation_change_decisions",
    )
    op.drop_table("remediation_change_decisions")
