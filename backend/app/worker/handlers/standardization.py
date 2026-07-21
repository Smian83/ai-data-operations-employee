"""Module 7 CSV data standardization execution handler. See
docs/module-7-data-standardization-engine-design.md for the full design.

KNOWN LIMITATION (see PROJECT_CONTEXT.md / the Module 7 completion
report): StandardizationConfig's five scalar settings (default_country,
default_currency, date_output_format, boolean_output_form,
numeric_locale) have no persistence or API surface yet -- the approved
design's Database Changes section (Section 3) only specifies
standardization_column_mappings and standardization_lookup_entries, both
fully implemented and wired below. Every run currently uses an
all-defaults StandardizationConfig(), so phone/state_province/postal_code
without a resolvable country column, ambiguous '$' currency, and
locale-ambiguous numeric values are conservatively left untouched --
consistent with the design's own "never guess" principle, just not yet
configurable beyond that conservative default.
"""
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

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.cleaning_run import CleaningRun
from app.models.data_source import DataSource
from app.models.enums import SourceType
from app.models.standardization_change import StandardizationChange
from app.models.standardization_column_mapping import StandardizationColumnMapping
from app.models.standardization_lookup_entry import StandardizationLookupEntry
from app.models.standardization_run import StandardizationRun
from app.profiling.csv_loader import CsvLoadError, load_csv
from app.standardization.classification import classify_columns
from app.standardization.engine import STANDARDIZATION_ENGINE_VERSION, standardize
from app.standardization.types import LookupTables, StandardizationConfig, StandardizationLimits
from app.profiling.types import CsvLimits
from app.worker.handlers.base import ExecutionContext, PermanentExecutionError


class StandardizationHandler:
    """Standardize the output of an APPROVED Module 6 CleaningRun, and
    persist one immutable StandardizationRun (+ bounded
    StandardizationChange rows) per standardization TaskRun.

    Persistence uses the same short independent transaction and
    unique-constraint-plus-refetch idempotency pattern CleaningHandler
    and CsvProfilingHandler both already use.
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
            raise PermanentExecutionError("standardization requires a data source")
        if data_source.source_type != SourceType.CSV_UPLOAD:
            raise PermanentExecutionError(
                f"STANDARDIZE is not implemented for source_type={data_source.source_type.value}"
            )

        source_task_run_id = context.task_run.source_task_run_id
        if source_task_run_id is None:
            raise PermanentExecutionError(
                "standardization requires source_task_run_id on the TaskRun"
            )

        db = self._session_factory()
        try:
            cleaning_run = db.execute(
                select(CleaningRun).where(
                    CleaningRun.task_run_id == source_task_run_id,
                    CleaningRun.organization_id == context.task_run.organization_id,
                )
            ).scalar_one_or_none()
            if cleaning_run is None:
                raise PermanentExecutionError(
                    "standardization requires a completed cleaning run for source_task_run_id"
                )
            if cleaning_run.status != "approved":
                raise PermanentExecutionError(
                    "standardization requires an APPROVED cleaning run for "
                    f"source_task_run_id (current status: {cleaning_run.status})"
                )

            existing = db.execute(
                select(StandardizationRun).where(
                    StandardizationRun.task_run_id == context.task_run.id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return (
                    f"standardization run already exists: standardization_run_id={existing.id} "
                    f"changes={existing.total_changes_count} status={existing.status}"
                )

            settings = get_settings()
            csv_limits = self._csv_limits(settings)
            try:
                # cleaning_run.output_file_path is never client-supplied --
                # it was computed and written entirely server-side by
                # CleaningHandler, already scoped to
                # CSV_OUTPUT_ROOT/{organization_id}/ at write time, and
                # cleaning_run itself was just looked up scoped to this
                # exact organization_id above. Unlike Module 5/6's
                # resolve_source_path (which validates untrusted,
                # client-supplied connection_metadata.file_path), no
                # additional path-escape validation is needed for a
                # value this handler never received from a client.
                loaded = load_csv(Path(cleaning_run.output_file_path), csv_limits)
            except CsvLoadError as exc:
                raise PermanentExecutionError(str(exc)) from exc

            column_overrides = self._load_column_overrides(
                db, context.task_run.organization_id, data_source.id
            )
            field_types = classify_columns(loaded.headers, column_overrides)

            lookup_tables = self._load_lookup_tables(db, context.task_run.organization_id)
            config = StandardizationConfig()
            limits = StandardizationLimits(
                max_persisted_changes=settings.standardization_max_persisted_changes
            )

            result = standardize(loaded.rows, loaded.headers, field_types, lookup_tables, config, limits)

            output_bytes = _serialize_csv(loaded.headers, result.standardized_rows)
            output_sha256 = hashlib.sha256(output_bytes).hexdigest()

            # Tenant-scoped output root, distinct from BOTH CSV_INPUT_ROOT
            # and CSV_OUTPUT_ROOT -- the Module 6 output being standardized
            # is never opened for writing anywhere in this handler.
            output_dir = Path(settings.csv_standardized_root) / str(data_source.organization_id)
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / f"{context.task_run.id}.csv"
            output_path.write_bytes(output_bytes)

            standardization_run = StandardizationRun(
                id=uuid.uuid4(),
                organization_id=context.task_run.organization_id,
                task_run_id=context.task_run.id,
                task_id=context.task.id,
                data_source_id=data_source.id,
                source_task_run_id=source_task_run_id,
                output_file_path=str(output_path),
                output_sha256=output_sha256,
                row_count=len(result.standardized_rows),
                total_changes_count=result.total_changes_count,
                changes_by_rule=result.changes_by_rule,
                confidence_score=result.confidence_score,
                standardization_engine_version=STANDARDIZATION_ENGINE_VERSION,
                status="pending_review",
            )
            db.add(standardization_run)
            for change in result.changes:
                db.add(
                    StandardizationChange(
                        organization_id=context.task_run.organization_id,
                        standardization_run_id=standardization_run.id,
                        row_index=change.row_index,
                        column_name=change.column_name,
                        field_type=change.field_type,
                        original_value=change.original_value,
                        standardized_value=change.standardized_value,
                        rule_name=change.rule_name,
                        rule_version=change.rule_version,
                        reason=change.reason,
                        confidence_score=change.confidence,
                    )
                )

            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = db.execute(
                    select(StandardizationRun).where(
                        StandardizationRun.task_run_id == context.task_run.id
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise
                standardization_run = existing
            else:
                db.refresh(standardization_run)

            return (
                f"standardization run created: standardization_run_id={standardization_run.id} "
                f"changes={standardization_run.total_changes_count} "
                f"confidence={standardization_run.confidence_score:.2f} "
                f"status={standardization_run.status}"
            )
        finally:
            db.close()

    @staticmethod
    def _load_column_overrides(
        db: Session, organization_id: uuid.UUID, data_source_id: uuid.UUID
    ) -> dict[str, str]:
        """Data-source-specific mappings take precedence over org-wide
        (data_source_id IS NULL) mappings for the same column name --
        resolved here by applying org-wide first, then overwriting with
        data-source-specific."""
        rows = db.execute(
            select(StandardizationColumnMapping).where(
                StandardizationColumnMapping.organization_id == organization_id,
                StandardizationColumnMapping.is_active.is_(True),
            )
        ).scalars().all()
        overrides: dict[str, str] = {}
        for row in rows:
            if row.data_source_id is None:
                overrides.setdefault(row.column_name.strip().lower(), row.field_type)
        for row in rows:
            if row.data_source_id == data_source_id:
                overrides[row.column_name.strip().lower()] = row.field_type
        return overrides

    @staticmethod
    def _load_lookup_tables(db: Session, organization_id: uuid.UUID) -> LookupTables:
        rows = db.execute(
            select(StandardizationLookupEntry).where(
                StandardizationLookupEntry.organization_id == organization_id,
                StandardizationLookupEntry.is_active.is_(True),
            )
        ).scalars().all()
        scoped: dict[str, dict[str, str]] = {}
        global_: dict[str, str] = {}
        for row in rows:
            key = row.lookup_key.strip().lower()
            if row.field_type is None:
                global_[key] = row.lookup_value
            else:
                scoped.setdefault(row.field_type, {})[key] = row.lookup_value
        return LookupTables(scoped=scoped, global_=global_)


def _serialize_csv(headers: list[str], rows: list[list[str]]) -> bytes:
    """Deterministic CSV serialization: csv.writer's default dialect
    applies the same quoting rules to the same input every time."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")
