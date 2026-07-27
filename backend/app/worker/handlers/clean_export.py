"""Module 19 Phase 3: CleanExportHandler — the worker entry point for
CLEAN_EXPORT TaskRuns.

When a TaskRun with task_type=CLEAN_EXPORT is dispatched to the worker,
this handler runs ExportService synchronously and returns a summary string.

Worker-triggered clean exports always produce CSV output and use the
TaskRun.id as the idempotency_key (one clean export per CLEAN_EXPORT
TaskRun, same once-per-run idempotency pattern every prior handler uses).

For format selection and custom idempotency keys, use the API directly:
  POST /jobs/{job_id}/exports
which triggers ExportService synchronously within the API request.

Algorithm:
  1. Verify data_source is present (RESTRICT: CLEAN_EXPORT requires one).
  2. Build a CleanExportRequest from the ExecutionContext.
  3. Call ExportService.run() -- all eligibility, loading, writing, and
     DB persistence happens there.
  4. Return a summary string describing the result.
"""
from __future__ import annotations

from collections.abc import Callable

from sqlalchemy.orm import Session

from sqlalchemy import select

from app.clean_export.service import ExportService
from app.clean_export.types import CleanExportRequest
from app.db.session import SessionLocal
from app.models.clean_export import CleanExport
from app.worker.handlers.base import ExecutionContext, PermanentExecutionError
from app.rules.handler_utils import load_resolved_rules, write_rule_set_run


class CleanExportHandler:
    """Process a CLEAN_EXPORT TaskRun via ExportService.

    One CleanExport record per CLEAN_EXPORT TaskRun. idempotency_key is
    derived from the TaskRun.id so re-dispatching the same TaskRun returns
    the existing export without re-running the export pipeline.
    """

    def __init__(
        self,
        session_factory: Callable[[], Session] = SessionLocal,
        export_service: ExportService | None = None,
    ) -> None:
        self._session_factory = session_factory
        self._export_service = export_service or ExportService()

    def execute(self, context: ExecutionContext) -> str:
        data_source = context.data_source
        if data_source is None:
            raise PermanentExecutionError(
                "clean_export requires a data source on the Task"
            )

        organization_id = context.task_run.organization_id
        job_id = context.task.id
        data_source_id = data_source.id

        # idempotency_key derived from TaskRun.id -- one export per TaskRun.
        idempotency_key = str(context.task_run.id)

        request = CleanExportRequest(
            organization_id=organization_id,
            job_id=job_id,
            data_source_id=data_source_id,
            format="csv",          # worker-triggered exports default to CSV
            idempotency_key=idempotency_key,
            dataset_version=None,  # auto-select latest PASS/PASS_WITH_WARNINGS
        )

        db = self._session_factory()
        try:
            # Module 21: load resolved business rules before running export
            resolved_rules = load_resolved_rules(db, organization_id, data_source_id)
            rr = resolved_rules.resolved_rules

            clean_export = self._export_service.run(db, request)

            # Module 21: if the export completed, check business rule constraints
            if clean_export.status == "completed":
                require_zero_critical = rr.get("export.require_zero_critical", False)
                min_quality_score = rr.get("export.min_quality_score", 0.0)
                # Check against QCR overall_score if available
                if require_zero_critical or min_quality_score > 0.0:
                    # Load the QCR for this export to check score
                    if hasattr(clean_export, "dataset_version") and clean_export.dataset_version:
                        from app.models.quality_control_run import QualityControlRun
                        qcr = db.execute(
                            select(QualityControlRun).where(
                                QualityControlRun.id == clean_export.dataset_version,
                                QualityControlRun.organization_id == organization_id,
                            )
                        ).scalar_one_or_none()
                        if qcr is not None:
                            score_fraction = (qcr.overall_score or 0.0) / 100.0
                            if require_zero_critical and qcr.blocking_count > 0:
                                clean_export.status = "blocked"
                                clean_export.failure_reason = "business_rule:require_zero_critical"
                                db.commit()
                            elif score_fraction < min_quality_score:
                                clean_export.status = "blocked"
                                clean_export.failure_reason = f"business_rule:min_quality_score:{score_fraction:.3f}<{min_quality_score}"
                                db.commit()

            # Module 21: write audit row
            try:
                write_rule_set_run(
                    db,
                    organization_id,
                    resolved_rules,
                    "clean_export",
                    clean_export.id,
                )
                db.commit()
            except Exception:
                db.rollback()
        finally:
            db.close()

        return (
            f"clean export {clean_export.status}: "
            f"clean_export_id={clean_export.id} "
            f"format={clean_export.format} "
            f"status={clean_export.status}"
            + (
                f" checksum={clean_export.checksum!r} "
                f"rows={clean_export.row_count}"
                if clean_export.status == "completed"
                else f" failure_reason={clean_export.failure_reason!r}"
            )
        )
