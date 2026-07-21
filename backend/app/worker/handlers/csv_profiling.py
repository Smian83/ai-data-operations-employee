"""Module 5 CSV ingestion and profiling execution handler."""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.data_profile import DataProfile
from app.models.enums import SourceType
from app.profiling.csv_loader import CsvLoadError, load_csv, resolve_source_path
from app.profiling.csv_profiler import profile_csv
from app.profiling.types import CsvLimits
from app.worker.handlers.base import ExecutionContext, PermanentExecutionError


class CsvProfilingHandler:
    """Profile a CSV_UPLOAD source and persist one immutable result per run.

    Persistence uses a short independent transaction rather than changing the
    Module 4 ``ExecutionContext`` contract. The unique ``task_run_id`` makes
    retries idempotent: if the profile committed but success reporting was
    interrupted, the next attempt returns the existing profile without
    repeating the database side effect.
    """

    def __init__(self, session_factory: Callable[[], Session] = SessionLocal) -> None:
        self._session_factory = session_factory

    @staticmethod
    def _limits() -> CsvLimits:
        settings = get_settings()
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
            raise PermanentExecutionError("CSV profiling requires a data source")
        if data_source.source_type != SourceType.CSV_UPLOAD:
            raise PermanentExecutionError(
                f"SYNC is not implemented for source_type={data_source.source_type.value}"
            )

        configured_path = data_source.connection_metadata.get("file_path")
        if not isinstance(configured_path, str):
            raise PermanentExecutionError(
                "CSV data source requires string connection_metadata.file_path"
            )

        settings = get_settings()
        try:
            # Tenant isolation: every organization is confined to its own
            # subdirectory of CSV_INPUT_ROOT (CSV_INPUT_ROOT/{organization_id}/),
            # not the shared root. Without this, resolve_source_path's
            # traversal-escape check alone would still let any org's
            # file_path reference any file under the shared root -- it only
            # ever guaranteed a path couldn't leave CSV_INPUT_ROOT, never
            # that it couldn't leave the CALLING org's slice of it. Scoping
            # the root itself to the data source's organization_id closes
            # that gap while reusing the exact same escape check unchanged.
            tenant_root = Path(settings.csv_input_root) / str(data_source.organization_id)
            path = resolve_source_path(tenant_root, configured_path)
            limits = self._limits()
            result = profile_csv(load_csv(path, limits), limits)
        except CsvLoadError as exc:
            raise PermanentExecutionError(str(exc)) from exc

        db = self._session_factory()
        try:
            existing = db.execute(
                select(DataProfile).where(DataProfile.task_run_id == context.task_run.id)
            ).scalar_one_or_none()
            if existing is not None:
                return (
                    f"csv profile already exists: profile_id={existing.id} "
                    f"rows={existing.row_count} columns={existing.column_count}"
                )

            profile = DataProfile(
                organization_id=context.task_run.organization_id,
                task_run_id=context.task_run.id,
                task_id=context.task.id,
                data_source_id=data_source.id,
                source_filename=result.source_filename,
                source_size_bytes=result.source_size_bytes,
                source_sha256=result.source_sha256,
                detected_encoding=result.detected_encoding,
                delimiter=result.delimiter,
                row_count=result.row_count,
                column_count=result.column_count,
                duplicate_row_count=result.duplicate_row_count,
                missing_value_total=result.missing_value_total,
                column_profiles=result.column_profiles,
                structural_issues=result.structural_issues,
                limits_applied=result.limits_applied,
            )
            db.add(profile)
            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = db.execute(
                    select(DataProfile).where(DataProfile.task_run_id == context.task_run.id)
                ).scalar_one_or_none()
                if existing is None:
                    raise
                profile = existing
            else:
                db.refresh(profile)

            return (
                f"csv profile created: profile_id={profile.id} "
                f"rows={profile.row_count} columns={profile.column_count} "
                f"duplicates={profile.duplicate_row_count} "
                f"missing_values={profile.missing_value_total}"
            )
        finally:
            db.close()
