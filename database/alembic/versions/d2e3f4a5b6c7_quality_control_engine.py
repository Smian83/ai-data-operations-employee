"""quality control engine

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-07-25 16:00:00.000000

Module 18 (Quality Control Engine) — Phase 1.

Adds:
  1. TaskType enum value 'quality_ctrl' (PostgreSQL only -- SQLite uses plain
     VARCHAR with no migration-time enum change required, same reasoning as
     every prior enum-extension migration in this project).
  2. Table: quality_control_runs (one immutable summary row per QUALITY_CTRL
     TaskRun; two UNIQUE constraints: task_run_id for idempotency, and
     validation_run_id for one authoritative release decision per
     ValidationRun).
  3. Table: quality_findings (one finding row per quality category/rule
     evaluation; append-only, never updated or deleted; bounded per run).
  4. Table: quality_thresholds (configurable release thresholds and category
     weights; two-partial-unique-index pattern for org-wide vs data-source-
     specific scoping, same as RemediationColumnRule and
     IssueDetectionColumnRule).

Purely additive -- no existing table, column, constraint, index, or enum
value is modified. The entire downgrade() undoes this migration by dropping
all three new tables (and their indexes) in FK-safe order.

Key design decisions (see docs/module-18-quality-control-engine-design.md):

Two UNIQUE constraints on quality_control_runs:
  uq_quality_control_runs_task_run_id     -- idempotency gate: one row per
    QUALITY_CTRL TaskRun execution (IntegrityError → handler catch-and-
    refetch, same as every prior run-summary table).
  uq_quality_control_runs_validation_run_id -- one authoritative release
    decision per ValidationRun: a second QUALITY_CTRL TaskRun targeting the
    same ValidationRun hits an IntegrityError, handler returns the first
    run's row. Module 19 uses validation_run_id to resolve the decision.

Nullable overall_score:
  overall_score is Float, nullable. When NULL: no applicable categories
  (all registered categories were skipped); release_recommendation is
  always 'FAIL' in this case. CHECK constraint allows NULL OR [0, 100].

Two-partial-unique-index on quality_thresholds:
  uq_quality_thresholds_org_scope    -- UNIQUE(organization_id) WHERE
    data_source_id IS NULL (org-wide default).
  uq_quality_thresholds_org_ds_scope -- UNIQUE(organization_id, data_source_id)
    WHERE data_source_id IS NOT NULL (data-source-specific override).
  NULL != NULL under SQL uniqueness semantics; two partial indexes are
  required (same pattern as ix_remediation_column_rules_scoped_active /
  ix_remediation_column_rules_orgwide_active from Module 15's migration).

FK ondelete strategy:
  organization_id -> organizations.id           CASCADE (org delete wipes tenant)
  (org, task_run_id) -> task_runs               CASCADE (run delete cascades)
  (org, task_id) -> tasks                       RESTRICT (task delete blocked)
  (org, data_source_id) -> data_sources         RESTRICT (source delete blocked)
  (org, validation_run_id) -> validation_runs   RESTRICT (audit row must survive)
  (org, qc_run_id) -> quality_control_runs      CASCADE  (run delete cascades)
  source_issue_id -> issues.id                  RESTRICT (audit row must survive)
  remediation_change_id -> remediation_changes  RESTRICT (audit row must survive)
  (org, validation_result_id) -> vld_results    RESTRICT (audit row must survive)
  (org, data_source_id) -> data_sources         RESTRICT (threshold config blocked)

Identifier length audit (all pre-measured, all <= 63 bytes):
  uq_quality_control_runs_task_run_id             38
  uq_quality_control_runs_validation_run_id       42
  uq_quality_control_runs_org_id                  30
  fk_quality_control_runs_org                     27  (inline on column)
  fk_quality_control_runs_org_task_run            36
  fk_quality_control_runs_org_task                32
  fk_quality_control_runs_org_data_source         40
  fk_quality_control_runs_org_validation_run      45
  ck_quality_control_runs_score_range             34
  ck_quality_control_runs_total_findings_nonneg   45
  ck_quality_control_runs_blocking_nonneg         39
  ck_quality_control_runs_warning_nonneg          38
  ck_quality_control_runs_info_nonneg             34
  ck_quality_control_runs_counts_reconcile        40
  ck_quality_control_runs_recommendation_valid    45
  ix_quality_control_runs_org                     27
  ix_quality_control_runs_task_run                31
  ix_quality_control_runs_validation_run          35
  ix_quality_control_runs_remediation_run         36
  ix_quality_control_runs_recommendation          35
  uq_quality_findings_org_id                      26
  fk_quality_findings_org                         23  (inline on column)
  fk_quality_findings_org_qc_run                  30
  fk_quality_findings_source_issue                32
  fk_quality_findings_remediation_change          38
  fk_quality_findings_org_validation_result       42
  ck_quality_findings_severity_valid              34
  ck_quality_findings_outcome_valid               32
  ck_quality_findings_category_valid              32
  ck_quality_findings_affected_row_count_nonneg   45  (exactly 45 -- within limit)
  ix_quality_findings_org                         23
  ix_quality_findings_quality_control_run         35
  ix_quality_findings_org_category_severity       39
  ix_quality_findings_severity                    27
  ix_quality_findings_outcome                     26
  ix_quality_findings_created_at                  30
  fk_quality_thresholds_org                       27  (inline on column)
  fk_quality_thresholds_org_data_source           38
  uq_quality_thresholds_org_scope                 30
  uq_quality_thresholds_org_ds_scope              30
  ck_quality_thresholds_fail_score_range          38
  ck_quality_thresholds_pass_score_range          38
  ck_quality_thresholds_pass_gt_fail              32
  ck_quality_thresholds_failure_rate_range        40
  ck_quality_thresholds_skip_rate_range           36
  ck_quality_thresholds_high_sev_nonneg           36
  ck_quality_thresholds_crit_sev_nonneg           36
  ck_quality_thresholds_max_warnings_nonneg       41
  ck_quality_thresholds_version_nonneg            34
  ix_quality_thresholds_org                       27
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d2e3f4a5b6c7"
down_revision: Union[str, None] = "c1d2e3f4a5b6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # =========================================================================
    # task_type_enum: add 'quality_ctrl' to the existing native PostgreSQL
    # enum type. Sixth ALTER TYPE ... ADD VALUE in this project (after
    # d4e5f6a7b8c9's 'standardize', f1a2b3c4d5e6's 'match',
    # f7a8b9c0d1e2's 'detect', a9b0c1d2e3f4's 'remediate', and
    # c1d2e3f4a5b6's 'validate') -- same restriction applies: the new
    # label cannot be used within the same transaction it is added in, so
    # this runs in its own autocommit block.
    # =========================================================================
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE task_type_enum ADD VALUE IF NOT EXISTS 'quality_ctrl'"
            )

    # =========================================================================
    # quality_control_runs: one immutable summary row per QUALITY_CTRL TaskRun.
    # Structural sibling of validation_runs -- no status/approval columns,
    # since the engine never mutates anything for a human to approve.
    #
    # Two UNIQUE constraints:
    #   uq_quality_control_runs_task_run_id   -- idempotency gate.
    #   uq_quality_control_runs_validation_run_id -- one authoritative
    #     release decision per ValidationRun.
    #
    # overall_score is nullable Float -- NULL when no categories applicable.
    # =========================================================================
    op.create_table(
        "quality_control_runs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("task_run_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=False),
        sa.Column("validation_run_id", sa.Uuid(), nullable=False),
        sa.Column("remediation_run_id", sa.Uuid(), nullable=False),
        sa.Column("issue_detection_run_id", sa.Uuid(), nullable=False),
        sa.Column("data_profile_id", sa.Uuid(), nullable=False),
        sa.Column("quality_engine_version", sa.String(length=20), nullable=False),
        # Nullable: NULL when no categories apply (→ always FAIL).
        sa.Column("overall_score", sa.Float(), nullable=True),
        sa.Column("release_recommendation", sa.String(length=30), nullable=False),
        sa.Column("total_findings", sa.Integer(), nullable=False),
        sa.Column("blocking_count", sa.Integer(), nullable=False),
        sa.Column("warning_count", sa.Integer(), nullable=False),
        sa.Column("info_count", sa.Integer(), nullable=False),
        sa.Column("category_scores", sa.JSON(), nullable=False),
        sa.Column("category_statuses", sa.JSON(), nullable=False),
        sa.Column("category_weights_used", sa.JSON(), nullable=False),
        sa.Column("post_remediation_stats", sa.JSON(), nullable=False),
        sa.Column("execution_snapshot", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # ---- Primary key ----
        sa.PrimaryKeyConstraint("id"),
        # ---- Unique constraints ----
        sa.UniqueConstraint(
            "task_run_id",
            name="uq_quality_control_runs_task_run_id",
        ),
        sa.UniqueConstraint(
            "validation_run_id",
            name="uq_quality_control_runs_validation_run_id",
        ),
        sa.UniqueConstraint(
            "organization_id", "id",
            name="uq_quality_control_runs_org_id",
        ),
        # ---- Check constraints ----
        sa.CheckConstraint(
            "overall_score IS NULL OR "
            "(overall_score >= 0.0 AND overall_score <= 100.0)",
            name="ck_quality_control_runs_score_range",
        ),
        sa.CheckConstraint(
            "total_findings >= 0",
            name="ck_quality_control_runs_total_findings_nonneg",
        ),
        sa.CheckConstraint(
            "blocking_count >= 0",
            name="ck_quality_control_runs_blocking_nonneg",
        ),
        sa.CheckConstraint(
            "warning_count >= 0",
            name="ck_quality_control_runs_warning_nonneg",
        ),
        sa.CheckConstraint(
            "info_count >= 0",
            name="ck_quality_control_runs_info_nonneg",
        ),
        sa.CheckConstraint(
            "blocking_count + warning_count + info_count = total_findings",
            name="ck_quality_control_runs_counts_reconcile",
        ),
        sa.CheckConstraint(
            "release_recommendation IN ('PASS', 'PASS_WITH_WARNINGS', 'FAIL')",
            name="ck_quality_control_runs_recommendation_valid",
        ),
        # ---- FK → organizations (CASCADE) ----
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_quality_control_runs_org",
            ondelete="CASCADE",
        ),
        # ---- Composite FK → task_runs (CASCADE) ----
        sa.ForeignKeyConstraint(
            ["organization_id", "task_run_id"],
            ["task_runs.organization_id", "task_runs.id"],
            name="fk_quality_control_runs_org_task_run",
            ondelete="CASCADE",
        ),
        # ---- Composite FK → tasks (RESTRICT) ----
        sa.ForeignKeyConstraint(
            ["organization_id", "task_id"],
            ["tasks.organization_id", "tasks.id"],
            name="fk_quality_control_runs_org_task",
            ondelete="RESTRICT",
        ),
        # ---- Composite FK → data_sources (RESTRICT) ----
        sa.ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_quality_control_runs_org_data_source",
            ondelete="RESTRICT",
        ),
        # ---- Composite FK → validation_runs (RESTRICT) ----
        sa.ForeignKeyConstraint(
            ["organization_id", "validation_run_id"],
            ["validation_runs.organization_id", "validation_runs.id"],
            name="fk_quality_control_runs_org_validation_run",
            ondelete="RESTRICT",
        ),
    )

    # ---- quality_control_runs scalar indexes ----
    op.create_index(
        "ix_quality_control_runs_org",
        "quality_control_runs",
        ["organization_id"],
    )
    op.create_index(
        "ix_quality_control_runs_task_run",
        "quality_control_runs",
        ["task_run_id"],
    )
    op.create_index(
        "ix_quality_control_runs_validation_run",
        "quality_control_runs",
        ["validation_run_id"],
    )
    op.create_index(
        "ix_quality_control_runs_remediation_run",
        "quality_control_runs",
        ["remediation_run_id"],
    )
    op.create_index(
        "ix_quality_control_runs_recommendation",
        "quality_control_runs",
        ["release_recommendation"],
    )

    # =========================================================================
    # quality_findings: one finding row per quality category/rule evaluation.
    # Append-only, immutable. Bounded per run by QUALITY_MAX_PERSISTED_FINDINGS.
    # QualityControlRun.total_findings is always the true total from the engine.
    #
    # Three nullable provenance columns: source_issue_id, remediation_change_id,
    # validation_result_id. Set only when the finding traces to a specific
    # upstream row; RESTRICT FKs prevent upstream rows from disappearing.
    #
    # reason is Text() (unbounded) for future encryption compatibility.
    # =========================================================================
    op.create_table(
        "quality_findings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("quality_control_run_id", sa.Uuid(), nullable=False),
        sa.Column("category", sa.String(length=50), nullable=False),
        sa.Column("rule_name", sa.String(length=100), nullable=False),
        sa.Column("rule_version", sa.String(length=20), nullable=False),
        sa.Column("severity", sa.String(length=20), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),          # Text() for encryption compat
        sa.Column("affected_row_count", sa.Integer(), nullable=True),
        sa.Column("affected_column", sa.String(length=255), nullable=True),
        # Nullable provenance columns -- see module docstring
        sa.Column("source_issue_id", sa.Uuid(), nullable=True),
        sa.Column("remediation_change_id", sa.Uuid(), nullable=True),
        sa.Column("validation_result_id", sa.Uuid(), nullable=True),
        sa.Column("quality_engine_version", sa.String(length=20), nullable=False),
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
            "organization_id", "id", name="uq_quality_findings_org_id"
        ),
        # ---- Check constraints ----
        sa.CheckConstraint(
            "severity IN ('info', 'warning', 'blocking')",
            name="ck_quality_findings_severity_valid",
        ),
        sa.CheckConstraint(
            "outcome IN ('passed', 'failed', 'skipped')",
            name="ck_quality_findings_outcome_valid",
        ),
        sa.CheckConstraint(
            "category IN ("
            "'completeness', 'uniqueness', 'validity', 'consistency', "
            "'referential_integrity', 'business_rule_compliance', "
            "'unresolved_risk', 'validation_coverage')",
            name="ck_quality_findings_category_valid",
        ),
        sa.CheckConstraint(
            "affected_row_count IS NULL OR affected_row_count >= 0",
            name="ck_quality_findings_affected_row_count_nonneg",
        ),
        # ---- FK → organizations (CASCADE) ----
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_quality_findings_org",
            ondelete="CASCADE",
        ),
        # ---- Composite FK → quality_control_runs (CASCADE) ----
        sa.ForeignKeyConstraint(
            ["organization_id", "quality_control_run_id"],
            ["quality_control_runs.organization_id", "quality_control_runs.id"],
            name="fk_quality_findings_org_qc_run",
            ondelete="CASCADE",
        ),
        # ---- Simple FK → issues.id (RESTRICT) ----
        # issues has no UNIQUE(organization_id, id), so a composite FK is
        # not possible -- same situation as remediation_change_decisions.
        sa.ForeignKeyConstraint(
            ["source_issue_id"],
            ["issues.id"],
            name="fk_quality_findings_source_issue",
            ondelete="RESTRICT",
        ),
        # ---- Simple FK → remediation_changes.id (RESTRICT) ----
        sa.ForeignKeyConstraint(
            ["remediation_change_id"],
            ["remediation_changes.id"],
            name="fk_quality_findings_remediation_change",
            ondelete="RESTRICT",
        ),
        # ---- Composite FK → validation_results (RESTRICT) ----
        sa.ForeignKeyConstraint(
            ["organization_id", "validation_result_id"],
            ["validation_results.organization_id", "validation_results.id"],
            name="fk_quality_findings_org_validation_result",
            ondelete="RESTRICT",
        ),
    )

    # ---- quality_findings scalar indexes ----
    op.create_index(
        "ix_quality_findings_org",
        "quality_findings",
        ["organization_id"],
    )
    op.create_index(
        "ix_quality_findings_quality_control_run",
        "quality_findings",
        ["quality_control_run_id"],
    )
    op.create_index(
        "ix_quality_findings_severity",
        "quality_findings",
        ["severity"],
    )
    op.create_index(
        "ix_quality_findings_outcome",
        "quality_findings",
        ["outcome"],
    )
    op.create_index(
        "ix_quality_findings_created_at",
        "quality_findings",
        ["created_at"],
    )
    # Composite: supports API filters by category+severity without table scan.
    op.create_index(
        "ix_quality_findings_org_category_severity",
        "quality_findings",
        ["organization_id", "category", "severity"],
    )

    # =========================================================================
    # quality_thresholds: configurable release thresholds and category weights.
    # Two-partial-unique-index pattern (NULL != NULL under SQL uniqueness):
    #   uq_quality_thresholds_org_scope    -- org-wide default row.
    #   uq_quality_thresholds_org_ds_scope -- data-source-specific override.
    #
    # Missing threshold → handler uses built-in defaults (not an error).
    # pass_score_threshold must be strictly > fail_score_threshold.
    # All rate columns are [0.0, 1.0]; all count columns are >= 0.
    # =========================================================================
    op.create_table(
        "quality_thresholds",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("organization_id", sa.Uuid(), nullable=False),
        sa.Column("data_source_id", sa.Uuid(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("fail_score_threshold", sa.Float(), nullable=False),
        sa.Column("pass_score_threshold", sa.Float(), nullable=False),
        sa.Column("max_validation_failure_rate", sa.Float(), nullable=False),
        sa.Column("max_validation_skip_rate", sa.Float(), nullable=False),
        sa.Column("max_high_severity_unresolved", sa.Integer(), nullable=False),
        sa.Column("max_critical_severity_unresolved", sa.Integer(), nullable=False),
        sa.Column("max_warnings_for_clean_pass", sa.Integer(), nullable=False),
        sa.Column("category_weights", sa.JSON(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        # ---- Primary key ----
        sa.PrimaryKeyConstraint("id"),
        # ---- Check constraints ----
        sa.CheckConstraint(
            "fail_score_threshold >= 0.0 AND fail_score_threshold <= 100.0",
            name="ck_quality_thresholds_fail_score_range",
        ),
        sa.CheckConstraint(
            "pass_score_threshold >= 0.0 AND pass_score_threshold <= 100.0",
            name="ck_quality_thresholds_pass_score_range",
        ),
        sa.CheckConstraint(
            "pass_score_threshold > fail_score_threshold",
            name="ck_quality_thresholds_pass_gt_fail",
        ),
        sa.CheckConstraint(
            "max_validation_failure_rate >= 0.0 AND "
            "max_validation_failure_rate <= 1.0",
            name="ck_quality_thresholds_failure_rate_range",
        ),
        sa.CheckConstraint(
            "max_validation_skip_rate >= 0.0 AND "
            "max_validation_skip_rate <= 1.0",
            name="ck_quality_thresholds_skip_rate_range",
        ),
        sa.CheckConstraint(
            "max_high_severity_unresolved >= 0",
            name="ck_quality_thresholds_high_sev_nonneg",
        ),
        sa.CheckConstraint(
            "max_critical_severity_unresolved >= 0",
            name="ck_quality_thresholds_crit_sev_nonneg",
        ),
        sa.CheckConstraint(
            "max_warnings_for_clean_pass >= 0",
            name="ck_quality_thresholds_max_warnings_nonneg",
        ),
        sa.CheckConstraint(
            "version >= 0",
            name="ck_quality_thresholds_version_nonneg",
        ),
        # ---- FK → organizations (CASCADE) ----
        sa.ForeignKeyConstraint(
            ["organization_id"],
            ["organizations.id"],
            name="fk_quality_thresholds_org",
            ondelete="CASCADE",
        ),
        # ---- Composite FK → data_sources (RESTRICT) ----
        # Only meaningful when data_source_id IS NOT NULL. When NULL, this
        # FK is satisfied vacuously (no referenced row required).
        sa.ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_quality_thresholds_org_data_source",
            ondelete="RESTRICT",
        ),
    )

    # ---- quality_thresholds scalar index ----
    op.create_index(
        "ix_quality_thresholds_org",
        "quality_thresholds",
        ["organization_id"],
    )

    # ---- Two-partial-unique-indexes for quality_thresholds ----
    # Org-wide default: at most one row per org when data_source_id IS NULL.
    op.create_index(
        "uq_quality_thresholds_org_scope",
        "quality_thresholds",
        ["organization_id"],
        unique=True,
        postgresql_where=sa.text("data_source_id IS NULL"),
        sqlite_where=sa.text("data_source_id IS NULL"),
    )
    # Data-source-specific: at most one row per org+data_source when IS NOT NULL.
    op.create_index(
        "uq_quality_thresholds_org_ds_scope",
        "quality_thresholds",
        ["organization_id", "data_source_id"],
        unique=True,
        postgresql_where=sa.text("data_source_id IS NOT NULL"),
        sqlite_where=sa.text("data_source_id IS NOT NULL"),
    )


def downgrade() -> None:
    # Drop quality_findings indexes and table first (child of quality_control_runs)
    op.drop_index(
        "ix_quality_findings_org_category_severity",
        table_name="quality_findings",
    )
    op.drop_index(
        "ix_quality_findings_created_at",
        table_name="quality_findings",
    )
    op.drop_index(
        "ix_quality_findings_outcome",
        table_name="quality_findings",
    )
    op.drop_index(
        "ix_quality_findings_severity",
        table_name="quality_findings",
    )
    op.drop_index(
        "ix_quality_findings_quality_control_run",
        table_name="quality_findings",
    )
    op.drop_index(
        "ix_quality_findings_org",
        table_name="quality_findings",
    )
    op.drop_table("quality_findings")

    # Drop quality_control_runs indexes and table
    op.drop_index(
        "ix_quality_control_runs_recommendation",
        table_name="quality_control_runs",
    )
    op.drop_index(
        "ix_quality_control_runs_remediation_run",
        table_name="quality_control_runs",
    )
    op.drop_index(
        "ix_quality_control_runs_validation_run",
        table_name="quality_control_runs",
    )
    op.drop_index(
        "ix_quality_control_runs_task_run",
        table_name="quality_control_runs",
    )
    op.drop_index(
        "ix_quality_control_runs_org",
        table_name="quality_control_runs",
    )
    op.drop_table("quality_control_runs")

    # Drop quality_thresholds partial unique indexes and table
    op.drop_index(
        "uq_quality_thresholds_org_ds_scope",
        table_name="quality_thresholds",
    )
    op.drop_index(
        "uq_quality_thresholds_org_scope",
        table_name="quality_thresholds",
    )
    op.drop_index(
        "ix_quality_thresholds_org",
        table_name="quality_thresholds",
    )
    op.drop_table("quality_thresholds")

    # Note: ALTER TYPE ... DROP VALUE is not supported by PostgreSQL.
    # 'quality_ctrl' added to task_type_enum in upgrade() is permanent at
    # the database layer. The ORM and application layer will simply never
    # create a TaskRun with task_type='quality_ctrl' after this migration
    # is rolled back. Same documented constraint as every prior enum-extension
    # migration in this project.
