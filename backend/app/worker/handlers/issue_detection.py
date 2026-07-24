"""Module 14 Phase 2 issue-detection execution handler. Reads the raw,
tenant-scoped source CSV directly -- the same file
CsvProfilingHandler/DataProfile already reads under CSV_INPUT_ROOT/
{organization_id}/ -- and runs it through app.detection.engine.
detect_issues, entirely PRIOR TO and independent of any Module 6/7
cleaning or standardization (approved Module 14 correction #1: "Raw-data
placement is correct"). Persists one immutable IssueDetectionRun (+
bounded Issue rows) per DETECT TaskRun.

STRICTLY READ ONLY with respect to the source file: this handler only
ever opens it for reading via app.profiling.csv_loader.load_csv (the same
safe/bounded loader Module 5 uses), never writes to it, never derives a
cleaned/standardized output file the way CleaningHandler/
StandardizationHandler do (approved Module 14 correction #8 -- "Source
files and existing task outputs must remain completely untouched").

Persistence uses the same short independent transaction and
unique-constraint-plus-refetch idempotency pattern every prior handler
(CsvProfilingHandler, CleaningHandler, StandardizationHandler,
MatchHandler, ExportHandler) already uses."""
from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.detection.engine import DETECTION_ENGINE_VERSION, detect_issues
from app.detection.types import ColumnRuleConfig, DetectionDataset, DetectionLimits
from app.models.enums import ISSUE_SEVERITIES, SourceType
from app.models.issue import Issue
from app.models.issue_detection_column_rule import IssueDetectionColumnRule
from app.models.issue_detection_run import IssueDetectionRun
from app.profiling.csv_loader import CsvLoadError, load_csv, resolve_source_path
from app.profiling.types import CsvLimits
from app.worker.handlers.base import ExecutionContext, PermanentExecutionError


class IssueDetectionHandler:
    """Detect data-quality issues in a CSV_UPLOAD source and persist one
    immutable IssueDetectionRun (+ capped Issue rows) per DETECT TaskRun."""

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
            raise PermanentExecutionError("issue detection requires a data source")
        if data_source.source_type != SourceType.CSV_UPLOAD:
            raise PermanentExecutionError(
                f"DETECT is not implemented for source_type={data_source.source_type.value}"
            )

        configured_path = data_source.connection_metadata.get("file_path")
        if not isinstance(configured_path, str):
            raise PermanentExecutionError(
                "CSV data source requires string connection_metadata.file_path"
            )

        settings = get_settings()
        try:
            # Same per-tenant isolation as CsvProfilingHandler: confined to
            # CSV_INPUT_ROOT/{organization_id}/, never the shared root.
            tenant_root = Path(settings.csv_input_root) / str(data_source.organization_id)
            path = resolve_source_path(tenant_root, configured_path)
            csv_limits = self._csv_limits(settings)
            loaded = load_csv(path, csv_limits)
        except CsvLoadError as exc:
            raise PermanentExecutionError(str(exc)) from exc

        db = self._session_factory()
        try:
            existing = db.execute(
                select(IssueDetectionRun).where(
                    IssueDetectionRun.task_run_id == context.task_run.id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return (
                    f"issue detection run already exists: detection_run_id={existing.id} "
                    f"total_issues_found={existing.total_issues_found}"
                )

            column_rules = self._load_column_rules(
                db,
                context.task_run.organization_id,
                data_source.id,
                loaded.headers,
                settings.issue_detection_outlier_zscore_threshold,
            )
            dataset = DetectionDataset(
                headers=loaded.headers,
                rows=loaded.rows,
                structural_issues=loaded.structural_issues,
                column_rules=column_rules,
            )
            limits = DetectionLimits(
                max_persisted_issues=settings.issue_detection_max_persisted_issues
            )
            result = detect_issues(dataset, limits)

            # Every ISSUE_SEVERITIES key present, 0 if none found -- see
            # IssueDetectionRun.issues_by_severity's own docstring. The
            # pure engine only returns severities it actually saw; this
            # handler fills in the rest before persisting.
            issues_by_severity = {
                severity: result.issues_by_severity.get(severity, 0)
                for severity in ISSUE_SEVERITIES
            }

            detection_run = IssueDetectionRun(
                id=uuid.uuid4(),
                organization_id=context.task_run.organization_id,
                task_run_id=context.task_run.id,
                task_id=context.task.id,
                data_source_id=data_source.id,
                rows_scanned=result.rows_scanned,
                columns_scanned=result.columns_scanned,
                total_issues_found=result.total_issues_found,
                persisted_issue_count=result.persisted_issue_count,
                issues_by_severity=issues_by_severity,
                issues_by_type=result.issues_by_type,
                limits_applied={
                    "max_persisted_issues": limits.max_persisted_issues,
                    "default_outlier_zscore_threshold": (
                        settings.issue_detection_outlier_zscore_threshold
                    ),
                },
                detection_engine_version=DETECTION_ENGINE_VERSION,
            )
            db.add(detection_run)
            for finding in result.findings:
                db.add(
                    Issue(
                        organization_id=context.task_run.organization_id,
                        detection_run_id=detection_run.id,
                        row_number=finding.row_number,
                        column_name=finding.column_name,
                        issue_type=finding.issue_type,
                        severity=finding.severity,
                        original_value=finding.original_value,
                        suggested_fix=finding.suggested_fix,
                        confidence=finding.confidence,
                    )
                )

            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = db.execute(
                    select(IssueDetectionRun).where(
                        IssueDetectionRun.task_run_id == context.task_run.id
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise
                detection_run = existing
            else:
                db.refresh(detection_run)

            return (
                f"issue detection run created: detection_run_id={detection_run.id} "
                f"total_issues_found={detection_run.total_issues_found} "
                f"persisted_issue_count={detection_run.persisted_issue_count}"
            )
        finally:
            db.close()

    @staticmethod
    def _load_column_rules(
        db: Session,
        organization_id: uuid.UUID,
        data_source_id: uuid.UUID,
        headers: list[str],
        default_outlier_zscore_threshold: float,
    ) -> dict[str, ColumnRuleConfig]:
        """Data-source-specific rows take precedence over org-wide
        (data_source_id IS NULL) rows for the same column name -- the same
        resolution order StandardizationHandler._load_column_overrides
        already uses for StandardizationColumnMapping. Matching is
        case/whitespace-insensitive on the configured column_name (mirrors
        the two partial unique indexes' own lower(trim(column_name))
        expression) but the returned dict is keyed by the dataset's actual
        header strings, since that is what every app.detection.rules
        module looks up by."""
        rows = (
            db.execute(
                select(IssueDetectionColumnRule).where(
                    IssueDetectionColumnRule.organization_id == organization_id,
                    IssueDetectionColumnRule.is_active.is_(True),
                )
            )
            .scalars()
            .all()
        )
        org_wide: dict[str, IssueDetectionColumnRule] = {}
        scoped: dict[str, IssueDetectionColumnRule] = {}
        for row in rows:
            key = row.column_name.strip().lower()
            if row.data_source_id is None:
                org_wide[key] = row
            elif row.data_source_id == data_source_id:
                scoped[key] = row

        resolved: dict[str, ColumnRuleConfig] = {}
        for header in headers:
            key = header.strip().lower()
            row = scoped.get(key) or org_wide.get(key)
            if row is None:
                continue
            resolved[header] = ColumnRuleConfig(
                expected_type=row.expected_type,
                is_required=row.is_required,
                is_primary_key=row.is_primary_key,
                allowed_values=(
                    tuple(row.allowed_values) if row.allowed_values else None
                ),
                outlier_enabled=row.outlier_enabled,
                outlier_zscore_threshold=(
                    row.outlier_zscore_threshold
                    if row.outlier_zscore_threshold is not None
                    else default_outlier_zscore_threshold
                ),
                capitalization_check_enabled=row.capitalization_check_enabled,
            )
        return resolved
