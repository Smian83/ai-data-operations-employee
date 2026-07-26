"""Module 19: unit tests for CleanExportHandler.

Covers:
  - Missing data_source raises PermanentExecutionError
  - Delegates to ExportService.run() with correct CleanExportRequest
  - idempotency_key = str(task_run.id)
  - format defaults to "csv"
  - Summary string contains status and clean_export_id
  - Completed summary includes checksum + row_count
  - Blocked/failed summary includes failure_reason
"""
from __future__ import annotations

import uuid
from unittest.mock import MagicMock, call

import pytest

from app.clean_export.types import CleanExportRequest
from app.worker.handlers.base import PermanentExecutionError
from app.worker.handlers.clean_export import CleanExportHandler


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_context(
    *,
    task_id: uuid.UUID | None = None,
    task_run_id: uuid.UUID | None = None,
    org_id: uuid.UUID | None = None,
    data_source_id: uuid.UUID | None = None,
    has_data_source: bool = True,
):
    task_id = task_id or uuid.uuid4()
    task_run_id = task_run_id or uuid.uuid4()
    org_id = org_id or uuid.uuid4()
    ds_id = data_source_id or uuid.uuid4()

    context = MagicMock()
    context.task.id = task_id
    context.task_run.id = task_run_id
    context.task_run.organization_id = org_id

    if has_data_source:
        context.data_source = MagicMock()
        context.data_source.id = ds_id
    else:
        context.data_source = None

    return context


def _make_completed_export(export_id=None, checksum=None, row_count=10):
    export = MagicMock()
    export.id = export_id or uuid.uuid4()
    export.status = "completed"
    export.format = "csv"
    export.checksum = checksum or "a" * 64
    export.row_count = row_count
    export.failure_reason = None
    return export


def _make_blocked_export(export_id=None, reason="No approved ExportRun"):
    export = MagicMock()
    export.id = export_id or uuid.uuid4()
    export.status = "blocked"
    export.format = "csv"
    export.checksum = None
    export.row_count = None
    export.failure_reason = reason
    return export


def _make_failed_export(export_id=None, reason="io_error: disk full"):
    export = MagicMock()
    export.id = export_id or uuid.uuid4()
    export.status = "failed"
    export.format = "csv"
    export.checksum = None
    export.row_count = None
    export.failure_reason = reason
    return export


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestCleanExportHandlerMissingDataSource:

    def test_no_data_source_raises_permanent(self):
        mock_service = MagicMock()
        mock_db = MagicMock()
        handler = CleanExportHandler(
            session_factory=lambda: mock_db,
            export_service=mock_service,
        )
        context = _make_context(has_data_source=False)
        with pytest.raises(PermanentExecutionError, match="data source"):
            handler.execute(context)
        mock_service.run.assert_not_called()


class TestCleanExportHandlerRequest:

    def test_builds_correct_request(self):
        task_run_id = uuid.uuid4()
        org_id = uuid.uuid4()
        task_id = uuid.uuid4()
        ds_id = uuid.uuid4()

        completed_export = _make_completed_export()
        mock_service = MagicMock()
        mock_service.run.return_value = completed_export
        mock_db = MagicMock()

        handler = CleanExportHandler(
            session_factory=lambda: mock_db,
            export_service=mock_service,
        )
        context = _make_context(
            task_id=task_id,
            task_run_id=task_run_id,
            org_id=org_id,
            data_source_id=ds_id,
        )
        handler.execute(context)

        # Verify the CleanExportRequest passed to ExportService.run()
        actual_request = mock_service.run.call_args[0][1]
        assert isinstance(actual_request, CleanExportRequest)
        assert actual_request.organization_id == org_id
        assert actual_request.job_id == task_id
        assert actual_request.data_source_id == ds_id
        assert actual_request.format == "csv"
        assert actual_request.idempotency_key == str(task_run_id)
        assert actual_request.dataset_version is None

    def test_idempotency_key_is_task_run_id(self):
        task_run_id = uuid.uuid4()
        completed_export = _make_completed_export()
        mock_service = MagicMock()
        mock_service.run.return_value = completed_export
        mock_db = MagicMock()

        handler = CleanExportHandler(
            session_factory=lambda: mock_db,
            export_service=mock_service,
        )
        handler.execute(_make_context(task_run_id=task_run_id))
        request = mock_service.run.call_args[0][1]
        assert request.idempotency_key == str(task_run_id)

    def test_format_always_csv(self):
        completed_export = _make_completed_export()
        mock_service = MagicMock()
        mock_service.run.return_value = completed_export
        mock_db = MagicMock()

        handler = CleanExportHandler(
            session_factory=lambda: mock_db,
            export_service=mock_service,
        )
        handler.execute(_make_context())
        request = mock_service.run.call_args[0][1]
        assert request.format == "csv"

    def test_dataset_version_is_none(self):
        completed_export = _make_completed_export()
        mock_service = MagicMock()
        mock_service.run.return_value = completed_export
        mock_db = MagicMock()

        handler = CleanExportHandler(
            session_factory=lambda: mock_db,
            export_service=mock_service,
        )
        handler.execute(_make_context())
        request = mock_service.run.call_args[0][1]
        assert request.dataset_version is None


class TestCleanExportHandlerSummaryString:

    def test_completed_summary_contains_required_fields(self):
        export_id = uuid.uuid4()
        checksum = "d" * 64
        completed_export = _make_completed_export(
            export_id=export_id, checksum=checksum, row_count=42
        )
        mock_service = MagicMock()
        mock_service.run.return_value = completed_export
        mock_db = MagicMock()

        handler = CleanExportHandler(
            session_factory=lambda: mock_db,
            export_service=mock_service,
        )
        summary = handler.execute(_make_context())

        assert "completed" in summary
        assert str(export_id) in summary
        assert checksum in summary
        assert "42" in summary

    def test_blocked_summary_contains_failure_reason(self):
        reason = "No approved ExportRun found for this task"
        blocked_export = _make_blocked_export(reason=reason)
        mock_service = MagicMock()
        mock_service.run.return_value = blocked_export
        mock_db = MagicMock()

        handler = CleanExportHandler(
            session_factory=lambda: mock_db,
            export_service=mock_service,
        )
        summary = handler.execute(_make_context())

        assert "blocked" in summary
        assert reason in summary

    def test_failed_summary_contains_failure_reason(self):
        reason = "io_error: disk full"
        failed_export = _make_failed_export(reason=reason)
        mock_service = MagicMock()
        mock_service.run.return_value = failed_export
        mock_db = MagicMock()

        handler = CleanExportHandler(
            session_factory=lambda: mock_db,
            export_service=mock_service,
        )
        summary = handler.execute(_make_context())

        assert "failed" in summary
        assert reason in summary

    def test_summary_includes_format(self):
        completed_export = _make_completed_export()
        mock_service = MagicMock()
        mock_service.run.return_value = completed_export
        mock_db = MagicMock()

        handler = CleanExportHandler(
            session_factory=lambda: mock_db,
            export_service=mock_service,
        )
        summary = handler.execute(_make_context())
        assert "csv" in summary


class TestCleanExportHandlerDBLifecycle:

    def test_db_closed_on_success(self):
        completed_export = _make_completed_export()
        mock_service = MagicMock()
        mock_service.run.return_value = completed_export
        mock_db = MagicMock()

        handler = CleanExportHandler(
            session_factory=lambda: mock_db,
            export_service=mock_service,
        )
        handler.execute(_make_context())
        mock_db.close.assert_called_once()

    def test_db_closed_even_if_service_raises(self):
        mock_service = MagicMock()
        mock_service.run.side_effect = RuntimeError("unexpected error")
        mock_db = MagicMock()

        handler = CleanExportHandler(
            session_factory=lambda: mock_db,
            export_service=mock_service,
        )
        with pytest.raises(RuntimeError):
            handler.execute(_make_context())
        mock_db.close.assert_called_once()


class TestCleanExportHandlerRegistry:

    def test_registered_in_handler_registry(self):
        from app.models.enums import TaskType
        from app.worker.handlers import HANDLER_REGISTRY
        assert TaskType.CLEAN_EXPORT in HANDLER_REGISTRY
        assert isinstance(HANDLER_REGISTRY[TaskType.CLEAN_EXPORT], CleanExportHandler)
