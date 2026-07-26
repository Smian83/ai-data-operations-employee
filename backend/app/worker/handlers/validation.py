"""Module 17 Phase 3 validation execution handler. The only impure layer
in Module 17 -- app.validation.engine.validate itself remains pure, no
I/O. See docs/module-17-validation-engine-design.md for the full design.

Algorithm (six steps, mirroring RemediationHandler's own six-step pattern):

1. Resolve the upstream RemediationRun via source_task_run_id, scoped to
   organization_id (decision 4). Missing -> permanent failure. This is the
   only cross-table lookup before the idempotency gate.

2. Early-exit if a ValidationRun for this task_run_id already exists.
   Returns the existing summary without any further DB reads -- same pattern
   as IssueDetectionHandler and RemediationHandler.

3. Freeze the approved RemediationChange snapshot. Two-pass batch loading
   (SQLite-compatible; no DISTINCT ON / window functions):
     a. Load ALL RemediationChange rows for this remediation_run_id,
        org-scoped.
     b. Load ALL RemediationChangeDecision rows for those change_ids,
        org-scoped.
     c. In Python: per change_id, find the decision with the latest
        decision_timestamp (UUID tie-break for stability). Keep the change
        only if that latest decision is "approved".
   The snapshot is frozen after this step -- any decision added after this
   point does NOT affect the current run.

4. Load ValidationColumnConfig for every unique, non-null column_name that
   appears in the approved snapshot. Resolves RemediationColumnRule (Module
   15's own table, data-source-specific overrides org-wide) merged with
   IssueDetectionColumnRule.allowed_values (Module 14's table, read-only).
   Absent-from-both columns get no entry in the returned dict -- identical
   "absent = no gate satisfied" convention as RemediationHandler and every
   other handler.

5. Build ValidationChangeInput objects from the frozen snapshot. Call the
   pure engine with the snapshot, the column config, and the configured
   limits.

6. Persist ValidationRun + ValidationResult rows in exactly ONE transaction.
   The same IntegrityError-catch-and-refetch safety net every handler uses
   handles the race condition where two concurrent workers execute the same
   VALIDATE TaskRun and both try to commit simultaneously.

Security constraints (enforced here, never relaxed):
  - Organization-scoped on every query. The handler never reads a row from
    another organization, even if source_task_run_id resolves to one.
  - No writes outside ValidationRun and ValidationResult.
  - No mutation of Issue, RemediationChange, or RemediationChangeDecision.
  - No CSV file reads -- the handler works entirely from already-persisted
    DB rows.
"""
from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.issue_detection_column_rule import IssueDetectionColumnRule
from app.models.remediation_change import RemediationChange
from app.models.remediation_change_decision import RemediationChangeDecision
from app.models.remediation_column_rule import RemediationColumnRule
from app.models.remediation_run import RemediationRun
from app.models.validation_result import ValidationResult
from app.models.validation_run import ValidationRun
from app.validation.engine import VALIDATION_ENGINE_VERSION, validate
from app.validation.types import (
    ValidationChangeInput,
    ValidationColumnConfig,
    ValidationLimits,
)
from app.worker.handlers.base import ExecutionContext, PermanentExecutionError


class ValidationHandler:
    """Validate the approved RemediationChange proposals from an upstream
    Module 15 RemediationRun, and persist one immutable ValidationRun (+
    ValidationResult rows) per VALIDATE TaskRun.

    The approved-change snapshot is frozen immediately (Adjustment 1) and
    never re-queried during this run's lifetime. Persistence uses the same
    short independent transaction and unique-constraint-plus-refetch
    idempotency pattern every prior handler already uses.
    """

    def __init__(self, session_factory: Callable[[], Session] = SessionLocal) -> None:
        self._session_factory = session_factory

    def execute(self, context: ExecutionContext) -> str:
        data_source = context.data_source
        if data_source is None:
            raise PermanentExecutionError("validation requires a data source")

        source_task_run_id = context.task_run.source_task_run_id
        if source_task_run_id is None:
            raise PermanentExecutionError(
                "validation requires source_task_run_id on the TaskRun"
            )

        organization_id = context.task_run.organization_id

        db = self._session_factory()
        try:
            # Step 1: resolve the upstream RemediationRun, scoped to this
            # exact organization_id.  A source_task_run_id that belongs to
            # a different organization (or does not exist at all, or was
            # never a REMEDIATE run) is indistinguishable from "missing"
            # here, providing the same tenant-isolation guarantee as
            # RemediationHandler: it never leaks whether a given id exists
            # in another organization.
            remediation_run = db.execute(
                select(RemediationRun).where(
                    RemediationRun.task_run_id == source_task_run_id,
                    RemediationRun.organization_id == organization_id,
                )
            ).scalar_one_or_none()
            if remediation_run is None:
                raise PermanentExecutionError(
                    "validation requires a completed remediation run for "
                    "source_task_run_id"
                )

            # Step 2: idempotency short-circuit -- return existing run
            # summary without any further DB reads or engine calls.
            existing = db.execute(
                select(ValidationRun).where(
                    ValidationRun.task_run_id == context.task_run.id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return (
                    f"validation run already exists: "
                    f"validation_run_id={existing.id} "
                    f"approved_changes_considered={existing.approved_changes_considered} "
                    f"passed_count={existing.passed_count} "
                    f"failed_count={existing.failed_count} "
                    f"skipped_count={existing.skipped_count}"
                )

            # Step 3a: batch-load ALL RemediationChange rows for this run,
            # org-scoped. No N+1: single query, scalars fetched in one pass.
            change_rows = (
                db.execute(
                    select(RemediationChange).where(
                        RemediationChange.remediation_run_id == remediation_run.id,
                        RemediationChange.organization_id == organization_id,
                    )
                )
                .scalars()
                .all()
            )

            # Step 3b: batch-load ALL RemediationChangeDecision rows for
            # those change_ids, org-scoped. Two-pass approach avoids
            # DISTINCT ON / window functions for SQLite compatibility --
            # same "load all then filter in Python" pattern used throughout.
            change_ids = [row.id for row in change_rows]
            if change_ids:
                decision_rows = (
                    db.execute(
                        select(RemediationChangeDecision).where(
                            RemediationChangeDecision.remediation_change_id.in_(change_ids),
                            RemediationChangeDecision.organization_id == organization_id,
                        )
                    )
                    .scalars()
                    .all()
                )
            else:
                decision_rows = []

            # Step 3c: latest-row-wins per change_id, by decision_timestamp.
            # UUID string tie-break provides a fully stable ordering when
            # two decisions share an identical timestamp (rare but possible
            # in test fixtures that don't set explicit timestamps).
            latest: dict[uuid.UUID, RemediationChangeDecision] = {}
            for decision in decision_rows:
                cid = decision.remediation_change_id
                prev = latest.get(cid)
                if prev is None:
                    latest[cid] = decision
                    continue
                # Compare timestamps first (aware datetime comparison).
                dt_new = decision.decision_timestamp
                dt_prev = prev.decision_timestamp
                # Normalise naive datetimes to UTC for comparison if needed.
                if dt_new.tzinfo is None:
                    dt_new = dt_new.replace(tzinfo=timezone.utc)
                if dt_prev.tzinfo is None:
                    dt_prev = dt_prev.replace(tzinfo=timezone.utc)
                if dt_new > dt_prev or (
                    dt_new == dt_prev and str(decision.id) > str(prev.id)
                ):
                    latest[cid] = decision

            # Freeze the approved-change snapshot (Adjustment 1).
            approved_change_rows = [
                row
                for row in change_rows
                if latest.get(row.id) is not None
                and latest[row.id].decision == "approved"
            ]

            # Step 4: load ValidationColumnConfig, keyed by column_name.
            # Use only the column names that appear in the approved snapshot
            # (no CSV headers available -- validation works from DB rows only).
            unique_column_names = list(
                {row.column_name for row in approved_change_rows if row.column_name is not None}
            )
            column_config = self._load_column_config(
                db, organization_id, data_source.id, unique_column_names
            )

            # Step 5: build inputs and call the pure engine.
            changes = [
                ValidationChangeInput(
                    change_id=row.id,
                    source_issue_id=row.source_issue_id,
                    row_number=row.row_number,
                    column_name=row.column_name,
                    action=row.action,
                    original_value=row.original_value,
                    proposed_value=row.proposed_value,
                )
                for row in approved_change_rows
            ]

            settings = get_settings()
            limits = ValidationLimits(
                max_persisted_results=settings.validation_max_persisted_results
            )

            result = validate(changes, column_config, limits)

            # Step 6: persist ValidationRun + ValidationResult rows in
            # exactly ONE transaction.  No partial commits, no nested
            # transactions. The IntegrityError catch-and-refetch below is
            # the safety net for the concurrent-duplicate-worker race
            # (same shape as IssueDetectionHandler and RemediationHandler).
            validation_run = ValidationRun(
                id=uuid.uuid4(),
                organization_id=organization_id,
                task_run_id=context.task_run.id,
                task_id=context.task.id,
                data_source_id=data_source.id,
                remediation_run_id=remediation_run.id,
                approved_changes_considered=result.approved_changes_considered,
                passed_count=result.passed_count,
                failed_count=result.failed_count,
                skipped_count=result.skipped_count,
                results_by_rule=result.results_by_rule,
                validation_engine_version=VALIDATION_ENGINE_VERSION,
            )
            db.add(validation_run)

            for item in result.results:
                db.add(
                    ValidationResult(
                        organization_id=organization_id,
                        validation_run_id=validation_run.id,
                        remediation_run_id=remediation_run.id,
                        remediation_change_id=item.change_id,
                        source_issue_id=item.source_issue_id,
                        validation_rule=item.validation_rule,
                        outcome=item.outcome,
                        reason=item.reason,
                        original_value=item.original_value,
                        proposed_value=item.proposed_value,
                        validation_engine_version=VALIDATION_ENGINE_VERSION,
                        validation_rule_version=item.validation_rule_version,
                    )
                )

            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = db.execute(
                    select(ValidationRun).where(
                        ValidationRun.task_run_id == context.task_run.id
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise
                validation_run = existing
            else:
                db.refresh(validation_run)

            return (
                f"validation run created: "
                f"validation_run_id={validation_run.id} "
                f"approved_changes_considered={validation_run.approved_changes_considered} "
                f"passed_count={validation_run.passed_count} "
                f"failed_count={validation_run.failed_count} "
                f"skipped_count={validation_run.skipped_count}"
            )
        finally:
            db.close()

    @staticmethod
    def _load_column_config(
        db: Session,
        organization_id: uuid.UUID,
        data_source_id: uuid.UUID,
        column_names: list[str],
    ) -> dict[str, ValidationColumnConfig]:
        """Mirrors RemediationHandler._load_column_config exactly, but
        returns ValidationColumnConfig objects instead of
        RemediationColumnConfig objects, and accepts a list of column_names
        from the approved-change snapshot rather than CSV headers.

        Resolution precedence (same as every other column-config resolver
        in this project): data-source-specific row overrides org-wide row.
        A column absent from both tables gets no entry in the returned dict
        -- "absent = no gate satisfied, never guess" applies here too.
        """
        if not column_names:
            return {}

        remediation_rows = (
            db.execute(
                select(RemediationColumnRule).where(
                    RemediationColumnRule.organization_id == organization_id,
                    RemediationColumnRule.is_active.is_(True),
                )
            )
            .scalars()
            .all()
        )
        remediation_org_wide: dict[str, RemediationColumnRule] = {}
        remediation_scoped: dict[str, RemediationColumnRule] = {}
        for row in remediation_rows:
            key = row.column_name.strip().lower()
            if row.data_source_id is None:
                remediation_org_wide[key] = row
            elif row.data_source_id == data_source_id:
                remediation_scoped[key] = row

        detection_rows = (
            db.execute(
                select(IssueDetectionColumnRule).where(
                    IssueDetectionColumnRule.organization_id == organization_id,
                    IssueDetectionColumnRule.is_active.is_(True),
                )
            )
            .scalars()
            .all()
        )
        detection_org_wide: dict[str, IssueDetectionColumnRule] = {}
        detection_scoped: dict[str, IssueDetectionColumnRule] = {}
        for row in detection_rows:
            key = row.column_name.strip().lower()
            if row.data_source_id is None:
                detection_org_wide[key] = row
            elif row.data_source_id == data_source_id:
                detection_scoped[key] = row

        resolved: dict[str, ValidationColumnConfig] = {}
        for col_name in column_names:
            key = col_name.strip().lower()
            remediation_row = remediation_scoped.get(key) or remediation_org_wide.get(key)
            detection_row = detection_scoped.get(key) or detection_org_wide.get(key)
            if remediation_row is None and detection_row is None:
                continue
            resolved[col_name] = ValidationColumnConfig(
                source_date_format=(
                    remediation_row.source_date_format if remediation_row else None
                ),
                target_date_format=(
                    remediation_row.target_date_format if remediation_row else None
                ),
                default_country=(
                    remediation_row.default_country if remediation_row else None
                ),
                capitalization_target=(
                    remediation_row.capitalization_target if remediation_row else None
                ),
                allowed_values=(
                    tuple(detection_row.allowed_values)
                    if detection_row and detection_row.allowed_values
                    else None
                ),
            )
        return resolved
