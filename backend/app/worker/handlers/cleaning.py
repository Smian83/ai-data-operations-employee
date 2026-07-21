"""Module 6 CSV data cleaning execution handler. See
docs/module-6-data-cleaning-engine-design.md for the full design."""
from __future__ import annotations

import csv
import hashlib
import io
import uuid
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.cleaning.engine import CLEANING_ENGINE_VERSION, clean
from app.cleaning.types import CleaningLimits
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.cleaning_change import CleaningChange
from app.models.cleaning_run import CleaningRun
from app.models.data_profile import DataProfile
from app.models.enums import SourceType
from app.profiling.csv_loader import CsvLoadError, load_csv, resolve_source_path
from app.profiling.csv_profiler import profile_csv
from app.profiling.types import CsvLimits, LoadedCsv
from app.worker.handlers.base import ExecutionContext, PermanentExecutionError


class CleaningHandler:
    """Clean a CSV_UPLOAD source already profiled by a prior SYNC run, and
    persist one immutable CleaningRun (+ bounded CleaningChange rows) per
    cleaning TaskRun.

    Persistence uses a short independent transaction, matching
    CsvProfilingHandler's pattern exactly: the unique task_run_id makes
    retries idempotent via IntegrityError catch-and-refetch, without any
    change to the Module 4 ExecutionContext contract.
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
            raise PermanentExecutionError("cleaning requires a data source")
        if data_source.source_type != SourceType.CSV_UPLOAD:
            raise PermanentExecutionError(
                f"TRANSFORM is not implemented for source_type={data_source.source_type.value}"
            )

        source_task_run_id = context.task_run.source_task_run_id
        if source_task_run_id is None:
            raise PermanentExecutionError(
                "cleaning requires source_task_run_id on the TaskRun"
            )

        configured_path = data_source.connection_metadata.get("file_path")
        if not isinstance(configured_path, str):
            raise PermanentExecutionError(
                "CSV data source requires string connection_metadata.file_path"
            )

        db = self._session_factory()
        try:
            profile = db.execute(
                select(DataProfile).where(
                    DataProfile.task_run_id == source_task_run_id,
                    DataProfile.organization_id == context.task_run.organization_id,
                )
            ).scalar_one_or_none()
            if profile is None:
                raise PermanentExecutionError(
                    "cleaning requires a completed profile for source_task_run_id"
                )

            existing = db.execute(
                select(CleaningRun).where(CleaningRun.task_run_id == context.task_run.id)
            ).scalar_one_or_none()
            if existing is not None:
                return (
                    f"cleaning run already exists: cleaning_run_id={existing.id} "
                    f"changes={existing.total_changes_count} status={existing.status}"
                )

            settings = get_settings()
            csv_limits = self._csv_limits(settings)
            try:
                # Tenant isolation: same per-organization root pattern as
                # CsvProfilingHandler (Module 5's B1 fix) -- re-reads the
                # exact file already read for profiling, never a different
                # or previously-unseen one.
                tenant_input_root = Path(settings.csv_input_root) / str(
                    data_source.organization_id
                )
                path = resolve_source_path(tenant_input_root, configured_path)
                loaded = load_csv(path, csv_limits)
            except CsvLoadError as exc:
                raise PermanentExecutionError(str(exc)) from exc

            column_profiles = profile.column_profiles
            if len(column_profiles) != len(loaded.headers):
                raise PermanentExecutionError(
                    "profiled column count does not match the current source file "
                    "-- the file may have changed since it was profiled"
                )
            column_types = [column["inferred_type"] for column in column_profiles]

            cleaning_limits = CleaningLimits(
                max_persisted_changes=settings.cleaning_max_persisted_changes
            )
            result = clean(loaded.rows, loaded.headers, column_types, cleaning_limits)

            output_bytes = _serialize_csv(loaded.headers, result.cleaned_rows)
            output_sha256 = hashlib.sha256(output_bytes).hexdigest()

            # Tenant-scoped output root, distinct from CSV_INPUT_ROOT --
            # the source file is never opened for writing anywhere in this
            # handler. See the design doc Section 13.
            output_dir = Path(settings.csv_output_root) / str(data_source.organization_id)
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / f"{context.task_run.id}.csv"
            output_path.write_bytes(output_bytes)

            post_clean_loaded = LoadedCsv(
                path=output_path,
                source_size_bytes=len(output_bytes),
                source_sha256=output_sha256,
                detected_encoding=loaded.detected_encoding,
                delimiter=loaded.delimiter,
                headers=loaded.headers,
                rows=result.cleaned_rows,
                structural_issues=[],
            )
            post_clean_profile = profile_csv(post_clean_loaded, csv_limits)

            # Assigned explicitly (rather than relying on the model's
            # default=uuid.uuid4) because CleaningChange rows below need
            # cleaning_run.id BEFORE the flush that would otherwise
            # populate it -- a Python-side column default is only applied
            # at flush time, not at object-construction time.
            cleaning_run = CleaningRun(
                id=uuid.uuid4(),
                organization_id=context.task_run.organization_id,
                task_run_id=context.task_run.id,
                task_id=context.task.id,
                data_source_id=data_source.id,
                source_task_run_id=source_task_run_id,
                output_file_path=str(output_path),
                output_sha256=output_sha256,
                row_count=len(result.cleaned_rows),
                total_changes_count=result.total_changes_count,
                changes_by_rule=result.changes_by_rule,
                duplicate_row_count=result.duplicate_row_count,
                confidence_score=result.confidence_score,
                post_clean_row_count=post_clean_profile.row_count,
                post_clean_missing_value_total=post_clean_profile.missing_value_total,
                post_clean_duplicate_row_count=post_clean_profile.duplicate_row_count,
                cleaning_engine_version=CLEANING_ENGINE_VERSION,
                status="pending_review",
            )
            db.add(cleaning_run)
            for change in result.changes:
                db.add(
                    CleaningChange(
                        organization_id=context.task_run.organization_id,
                        cleaning_run_id=cleaning_run.id,
                        row_index=change.row_index,
                        column_name=change.column_name,
                        original_value=change.original_value,
                        cleaned_value=change.cleaned_value,
                        rule_name=change.rule_name,
                        reason=change.reason,
                        confidence_score=change.confidence,
                    )
                )

            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = db.execute(
                    select(CleaningRun).where(CleaningRun.task_run_id == context.task_run.id)
                ).scalar_one_or_none()
                if existing is None:
                    raise
                cleaning_run = existing
            else:
                db.refresh(cleaning_run)

            return (
                f"cleaning run created: cleaning_run_id={cleaning_run.id} "
                f"changes={cleaning_run.total_changes_count} "
                f"confidence={cleaning_run.confidence_score:.2f} "
                f"status={cleaning_run.status}"
            )
        finally:
            db.close()


def _serialize_csv(headers: list[str], rows: list[list[str]]) -> bytes:
    """Deterministic CSV serialization: csv.writer's default dialect
    applies the same quoting rules to the same input every time."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")
