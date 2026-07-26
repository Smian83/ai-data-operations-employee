"""Module 15 Phase 3 remediation execution handler. The only impure layer
in Module 15 -- app.remediation.engine.remediate itself remains pure, no
I/O. See docs/module-15-deterministic-cleaning-engine-design.md Section 5
for the exact six-step algorithm this handler implements:

1. Resolve the upstream IssueDetectionRun via source_task_run_id, scoped
   to organization_id (decision 4). Missing -> permanent failure.
2. Reject if IssueDetectionRun.source_sha256 IS NULL (predates tracking --
   permanent failure, explicit message).
3. Re-read the current source file (same tenant-scoped
   resolve_source_path/load_csv IssueDetectionHandler already uses) and
   compare its freshly computed sha256 to IssueDetectionRun.source_sha256.
   Mismatch -> permanent failure ("source data has changed since
   detection; re-run detection before remediation").
4. Fetch the Issue rows for that IssueDetectionRun.
5. Resolve RemediationColumnRule (data-source-specific overriding
   org-wide, same resolution order as every prior config table) merged
   with IssueDetectionColumnRule.allowed_values (read-only, never
   duplicated -- see RemediationColumnConfig's own docstring), and
   RemediationDatasetConfig for this data_source_id.
6. Call the pure engine, persist RemediationRun + RemediationChange rows
   in one short transaction (same unique-task_run_id, IntegrityError-
   catch-and-refetch idempotency pattern every handler uses).

Implementation note (not a behavior deviation): the existing-RemediationRun
short-circuit check is performed immediately after step 1/2 (upstream run
resolved, hash presence confirmed) but BEFORE step 3's source-file re-read
-- a retry of an already-completed REMEDIATE TaskRun returns the existing
summary without re-reading the CSV or re-fetching Issues at all, purely an
I/O-avoidance ordering choice. The final IntegrityError-catch-and-refetch
safety net (step 6) still exists underneath this for the race-condition
case, exactly like every prior handler.

STRICTLY READ ONLY with respect to the source file and every upstream
row: this handler never writes to the source CSV, never mutates any Issue
or IssueDetectionRun row, generates no remediated CSV output, and
performs no approval/rollback/apply logic of any kind -- Module 15's only
deliverable is the RemediationChange proposal rows themselves (see the
design doc Section 1/7)."""
from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.enums import SourceType
from app.models.issue import Issue
from app.models.issue_detection_column_rule import IssueDetectionColumnRule
from app.models.issue_detection_run import IssueDetectionRun
from app.models.remediation_change import RemediationChange
from app.models.remediation_column_rule import RemediationColumnRule
from app.models.remediation_dataset_config import RemediationDatasetConfig
from app.models.remediation_run import RemediationRun
from app.profiling.csv_loader import CsvLoadError, load_csv, resolve_source_path
from app.profiling.types import CsvLimits
from app.remediation.engine import REMEDIATION_ENGINE_VERSION, remediate
from app.remediation.types import (
    RemediationColumnConfig,
    RemediationDataset,
    RemediationDatasetConfigInput,
    RemediationIssueInput,
    RemediationLimits,
)
from app.worker.handlers.base import ExecutionContext, PermanentExecutionError


class RemediationHandler:
    """Propose deterministic corrections for the Issues found by an
    upstream Module 14 IssueDetectionRun, and persist one immutable
    RemediationRun (+ RemediationChange rows) per REMEDIATE TaskRun.

    Persistence uses the same short independent transaction and
    unique-constraint-plus-refetch idempotency pattern every prior handler
    already uses.
    """

    def __init__(self, session_factory: Callable[[], Session] = SessionLocal) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _csv_limits(settings) -> CsvLimits:
        return CsvLimits(
            max_file_size_bytes=settings.csv_max_file_size_bytes,
            max_rows=settings.csv_max_rows,
            max_columns=settings.csv_max_columns,
            max_cell_length=settings.csv_max_cell_length,
            max_distinct_values=settings.csv_max_distinct_values,
            max_sample_values=settings.csv_max_sample_values,
        )

    def execute(self, context: ExecutionContext) -> str:
        data_source = context.data_source
        if data_source is None:
            raise PermanentExecutionError("remediation requires a data source")
        if data_source.source_type != SourceType.CSV_UPLOAD:
            raise PermanentExecutionError(
                f"REMEDIATE is not implemented for source_type={data_source.source_type.value}"
            )

        source_task_run_id = context.task_run.source_task_run_id
        if source_task_run_id is None:
            raise PermanentExecutionError(
                "remediation requires source_task_run_id on the TaskRun"
            )

        organization_id = context.task_run.organization_id

        db = self._session_factory()
        try:
            # Step 1: resolve the upstream IssueDetectionRun, scoped to
            # this exact organization_id -- a source_task_run_id that
            # belongs to a different organization (or does not exist at
            # all, or was never a DETECT run) is indistinguishable from
            # "missing" here, which is exactly the tenant-isolation
            # guarantee this handler provides: it never leaks whether a
            # given id exists in another organization.
            detection_run = db.execute(
                select(IssueDetectionRun).where(
                    IssueDetectionRun.task_run_id == source_task_run_id,
                    IssueDetectionRun.organization_id == organization_id,
                )
            ).scalar_one_or_none()
            if detection_run is None:
                raise PermanentExecutionError(
                    "remediation requires a completed issue detection run for "
                    "source_task_run_id"
                )

            # Step 2: a NULL source_sha256 means this IssueDetectionRun
            # predates hash tracking (Module 15 Phase 1's additive
            # touch) -- there is no way to verify the source file has not
            # changed since, so remediation must never proceed against it.
            if detection_run.source_sha256 is None:
                raise PermanentExecutionError(
                    "issue detection run predates source hash tracking; "
                    "re-run detection before remediation"
                )

            existing = db.execute(
                select(RemediationRun).where(
                    RemediationRun.task_run_id == context.task_run.id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return (
                    f"remediation run already exists: remediation_run_id={existing.id} "
                    f"total_changes_count={existing.total_changes_count} "
                    f"issues_skipped_count={existing.issues_skipped_count}"
                )

            configured_path = data_source.connection_metadata.get("file_path")
            if not isinstance(configured_path, str):
                raise PermanentExecutionError(
                    "CSV data source requires string connection_metadata.file_path"
                )

            settings = get_settings()
            try:
                # Same per-tenant isolation as IssueDetectionHandler:
                # confined to CSV_INPUT_ROOT/{organization_id}/, never the
                # shared root. Opened for reading only -- never for
                # writing, at any point in this handler.
                tenant_root = Path(settings.csv_input_root) / str(organization_id)
                path = resolve_source_path(tenant_root, configured_path)
                csv_limits = self._csv_limits(settings)
                loaded = load_csv(path, csv_limits)
            except CsvLoadError as exc:
                raise PermanentExecutionError(str(exc)) from exc

            # Step 3: the current source file must be byte-identical to
            # what Module 14 scanned -- never a partial/fuzzy match, never
            # a "close enough" heuristic.
            if loaded.source_sha256 != detection_run.source_sha256:
                raise PermanentExecutionError(
                    "source data has changed since detection; re-run detection "
                    "before remediation"
                )

            # Step 4: fetch the Issue rows this IssueDetectionRun actually
            # persisted (already bounded by settings.
            # issue_detection_max_persisted_issues at write time -- never
            # re-capped here).
            issue_rows = (
                db.execute(
                    select(Issue).where(
                        Issue.detection_run_id == detection_run.id,
                        Issue.organization_id == organization_id,
                    )
                )
                .scalars()
                .all()
            )

            # Step 5: resolve configuration.
            column_config = self._load_column_config(
                db, organization_id, data_source.id, loaded.headers
            )
            dataset_config = self._load_dataset_config(db, organization_id, data_source.id)

            dataset = RemediationDataset(headers=loaded.headers, rows=loaded.rows)
            issues = [
                RemediationIssueInput(
                    issue_id=row.id,
                    row_number=row.row_number,
                    column_name=row.column_name,
                    issue_type=row.issue_type,
                    original_value=row.original_value,
                    suggested_fix=row.suggested_fix,
                )
                for row in issue_rows
            ]
            limits = RemediationLimits(
                max_persisted_changes=settings.remediation_max_persisted_changes
            )

            # Step 6: the pure engine call -- no I/O, no randomness,
            # deterministic given this exact input.
            result = remediate(dataset, issues, column_config, dataset_config, limits)

            skipped_by_reason: dict[str, int] = {}
            for skipped in result.skipped:
                skipped_by_reason[skipped.reason] = skipped_by_reason.get(skipped.reason, 0) + 1

            remediation_run = RemediationRun(
                id=uuid.uuid4(),
                organization_id=organization_id,
                task_run_id=context.task_run.id,
                task_id=context.task.id,
                data_source_id=data_source.id,
                source_task_run_id=source_task_run_id,
                issues_considered_count=result.issues_considered_count,
                total_changes_count=result.total_changes_count,
                issues_skipped_count=result.issues_skipped_count,
                changes_by_action=result.changes_by_action,
                skipped_by_reason=skipped_by_reason,
                remediation_engine_version=REMEDIATION_ENGINE_VERSION,
            )
            db.add(remediation_run)
            for proposal in result.changes:
                db.add(
                    RemediationChange(
                        organization_id=organization_id,
                        remediation_run_id=remediation_run.id,
                        source_issue_id=proposal.source_issue_id,
                        row_number=proposal.row_number,
                        column_name=proposal.column_name,
                        action=proposal.action,
                        original_value=proposal.original_value,
                        proposed_value=proposal.proposed_value,
                        reason=proposal.reason,
                        confidence=proposal.confidence,
                    )
                )

            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = db.execute(
                    select(RemediationRun).where(
                        RemediationRun.task_run_id == context.task_run.id
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise
                remediation_run = existing
            else:
                db.refresh(remediation_run)

            return (
                f"remediation run created: remediation_run_id={remediation_run.id} "
                f"issues_considered_count={remediation_run.issues_considered_count} "
                f"total_changes_count={remediation_run.total_changes_count} "
                f"issues_skipped_count={remediation_run.issues_skipped_count}"
            )
        finally:
            db.close()

    @staticmethod
    def _load_column_config(
        db: Session,
        organization_id: uuid.UUID,
        data_source_id: uuid.UUID,
        headers: list[str],
    ) -> dict[str, RemediationColumnConfig]:
        """Merges two independently-scoped config tables, both resolved
        with the same "data-source-specific row overrides org-wide row"
        precedence StandardizationHandler/IssueDetectionHandler already
        use: RemediationColumnRule (source_date_format/target_date_format/
        default_country/capitalization_target -- Module 15's own table)
        and IssueDetectionColumnRule.allowed_values (Module 14's table,
        read-only, never copied -- see RemediationColumnConfig's own
        docstring). A column absent from both simply gets no entry in the
        returned dict, matching every field's own "absent = no gate
        satisfied, never guess" convention."""
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

        resolved: dict[str, RemediationColumnConfig] = {}
        for header in headers:
            key = header.strip().lower()
            remediation_row = remediation_scoped.get(key) or remediation_org_wide.get(key)
            detection_row = detection_scoped.get(key) or detection_org_wide.get(key)
            if remediation_row is None and detection_row is None:
                continue
            resolved[header] = RemediationColumnConfig(
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

    @staticmethod
    def _load_dataset_config(
        db: Session, organization_id: uuid.UUID, data_source_id: uuid.UUID
    ) -> RemediationDatasetConfigInput:
        """RemediationDatasetConfig has no org-wide fallback (data_source_id
        is required on that table) -- absence of an active row for this
        exact data_source_id means both flags stay False, the documented
        default-off behavior, never inferred from any other data source's
        configuration."""
        row = db.execute(
            select(RemediationDatasetConfig).where(
                RemediationDatasetConfig.organization_id == organization_id,
                RemediationDatasetConfig.data_source_id == data_source_id,
                RemediationDatasetConfig.is_active.is_(True),
            )
        ).scalar_one_or_none()
        if row is None:
            return RemediationDatasetConfigInput()
        return RemediationDatasetConfigInput(
            remove_duplicate_rows_enabled=row.remove_duplicate_rows_enabled,
            remove_duplicate_primary_keys_enabled=row.remove_duplicate_primary_keys_enabled,
        )
