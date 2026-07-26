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

from app.clean_export.service import ExportService
from app.clean_export.types import CleanExportRequest
from app.db.session import SessionLocal
from app.worker.handlers.base import ExecutionContext, PermanentExecutionError


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
            clean_export = self._export_service.run(db, request)
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
