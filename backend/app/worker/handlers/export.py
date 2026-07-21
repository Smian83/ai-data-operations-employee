"""Module 9 data export execution handler. See
docs/module-9-data-export-engine-design.md for the full design.

ExportHandler consumes an APPROVED Module 8 MatchRun and materializes it
into an actual deduplicated output CSV -- the first module since Module 7
to write an output file, and the first module whose output has fewer
rows than its input, by deliberate, human-approved design. It never
opens the Module 7 standardized file, or any Module 8 row, for writing.

Provenance-column collision policy (required architectural
clarification): before any group-loading, materialization, or file
writing, the handler checks whether either reserved column name
(__aiops_canonical_record / __aiops_source_row_index) already exists in
the standardized input's header. If either does, this is a PERMANENT
failure -- no output file is written, no ExportRun row is persisted (not
even partially), and no rename/suffix/overwrite is ever attempted.

Determinism (required clarification): export_timestamp is written only
to the ExportRun database row, never into the CSV file itself, so two
independent EXPORT TaskRuns against identical approved input produce
byte-identical output files (same output_sha256, same
output_file_size_bytes). Retrying the same TaskRun returns the existing
ExportRun unchanged -- the file is not rewritten and export_timestamp is
not replaced.
"""
from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable
from csv import writer as csv_writer
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.db.session import SessionLocal
from app.export.engine import (
    EXPORT_CSV_FORMAT_VERSION,
    EXPORT_ENGINE_VERSION,
    find_reserved_column_collisions,
    materialize,
)
from app.export.types import ExportLimits, GroupInput
from app.models.enums import SourceType
from app.models.export_row_exclusion import ExportRowExclusion
from app.models.export_run import ExportRun
from app.models.match_decision import MatchDecision
from app.models.match_group import MatchGroup
from app.models.match_run import MatchRun
from app.models.standardization_run import StandardizationRun
from app.profiling.csv_loader import CsvLoadError, load_csv
from app.profiling.types import CsvLimits
from app.worker.handlers.base import ExecutionContext, PermanentExecutionError


class ExportHandler:
    """Materialize the output of an APPROVED Module 8 MatchRun, and
    persist one immutable ExportRun (+ bounded ExportRowExclusion rows)
    per EXPORT TaskRun.

    Persistence uses the same short independent transaction and
    unique-constraint-plus-refetch idempotency pattern every prior
    handler already uses.
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
            raise PermanentExecutionError("export requires a data source")
        if data_source.source_type != SourceType.CSV_UPLOAD:
            raise PermanentExecutionError(
                f"EXPORT is not implemented for source_type={data_source.source_type.value}"
            )

        source_task_run_id = context.task_run.source_task_run_id
        if source_task_run_id is None:
            raise PermanentExecutionError("export requires source_task_run_id on the TaskRun")

        db = self._session_factory()
        try:
            match_run = db.execute(
                select(MatchRun).where(
                    MatchRun.task_run_id == source_task_run_id,
                    MatchRun.organization_id == context.task_run.organization_id,
                )
            ).scalar_one_or_none()
            if match_run is None:
                raise PermanentExecutionError(
                    "export requires a completed match run for source_task_run_id"
                )
            if match_run.status != "approved":
                raise PermanentExecutionError(
                    "export requires an APPROVED match run for source_task_run_id "
                    f"(current status: {match_run.status})"
                )

            # Idempotency short-circuit -- BEFORE any load/collision-check/
            # materialize/write, so a retry of the same TaskRun never
            # rewrites the file or replaces export_timestamp (required
            # determinism clarification).
            existing = db.execute(
                select(ExportRun).where(ExportRun.task_run_id == context.task_run.id)
            ).scalar_one_or_none()
            if existing is not None:
                return (
                    f"export run already exists: export_run_id={existing.id} "
                    f"row_count={existing.row_count} status={existing.status}"
                )

            standardization_run = db.execute(
                select(StandardizationRun).where(
                    StandardizationRun.task_run_id == match_run.source_task_run_id,
                    StandardizationRun.organization_id == context.task_run.organization_id,
                )
            ).scalar_one_or_none()
            if standardization_run is None:
                # Defensive: MatchHandler already required this to exist
                # and be approved before match_run itself could exist.
                raise PermanentExecutionError(
                    "export requires the matched standardization run to still exist"
                )

            settings = get_settings()
            csv_limits = self._csv_limits(settings)
            try:
                # standardization_run.output_file_path is never
                # client-supplied -- same reasoning MatchHandler already
                # documents for this exact lookup.
                loaded = load_csv(Path(standardization_run.output_file_path), csv_limits)
            except CsvLoadError as exc:
                raise PermanentExecutionError(str(exc)) from exc

            if len(loaded.rows) != match_run.row_count:
                raise PermanentExecutionError(
                    "export found a row-count mismatch between the standardized "
                    "input and the match run being exported "
                    f"(standardized={len(loaded.rows)}, match_run={match_run.row_count})"
                )

            # Reserved provenance-column collision check -- MUST happen
            # before any group-loading, materialization, or file writing.
            # A collision is always a permanent failure; no rename,
            # suffix, or overwrite is ever attempted.
            collisions = find_reserved_column_collisions(loaded.headers)
            if collisions:
                raise PermanentExecutionError(
                    "export cannot proceed: the standardized input already contains "
                    "reserved column name(s) " + ", ".join(repr(c) for c in collisions) + " "
                    "-- rename the conflicting column upstream before exporting"
                )

            groups = self._load_groups(db, context.task_run.organization_id, match_run.id)

            limits = ExportLimits(
                max_persisted_exclusions=settings.export_max_persisted_exclusions
            )
            result = materialize(loaded.rows, loaded.headers, groups, limits)

            output_bytes = _serialize_csv(result.output_headers, result.output_rows)
            output_sha256 = hashlib.sha256(output_bytes).hexdigest()

            # Tenant-scoped output root, distinct from CSV_INPUT_ROOT,
            # CSV_OUTPUT_ROOT, and CSV_STANDARDIZED_ROOT -- the Module 7
            # standardized file being exported is never opened for
            # writing anywhere in this handler.
            output_dir = Path(settings.csv_exported_root) / str(data_source.organization_id)
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / f"{context.task_run.id}.csv"
            output_path.write_bytes(output_bytes)
            # Post-write filesystem stat -- an exact measurement of what
            # actually landed on disk, not an in-memory byte count.
            output_file_size_bytes = output_path.stat().st_size

            # DATABASE METADATA ONLY -- computed after the file write
            # completes, but never folded into output_bytes/output_sha256
            # above (which were already computed before this line).
            export_timestamp = datetime.now(timezone.utc)

            export_run = ExportRun(
                id=uuid.uuid4(),
                organization_id=context.task_run.organization_id,
                task_run_id=context.task_run.id,
                task_id=context.task.id,
                data_source_id=data_source.id,
                source_task_run_id=source_task_run_id,
                match_run_id=match_run.id,
                output_file_path=str(output_path),
                output_sha256=output_sha256,
                source_row_count=len(loaded.rows),
                row_count=result.row_count,
                excluded_row_count=result.excluded_row_count,
                duplicate_groups_materialized_count=result.duplicate_groups_materialized_count,
                output_file_size_bytes=output_file_size_bytes,
                output_column_count=result.output_column_count,
                export_timestamp=export_timestamp,
                csv_format_version=EXPORT_CSV_FORMAT_VERSION,
                export_engine_version=EXPORT_ENGINE_VERSION,
                status="pending_review",
            )
            db.add(export_run)

            for exclusion in result.exclusions:
                db.add(
                    ExportRowExclusion(
                        organization_id=context.task_run.organization_id,
                        export_run_id=export_run.id,
                        row_index=exclusion.row_index,
                        match_group_id=exclusion.match_group_id,
                        canonical_row_index=exclusion.canonical_row_index,
                        reason=exclusion.reason,
                        rule_version=EXPORT_ENGINE_VERSION,
                    )
                )

            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = db.execute(
                    select(ExportRun).where(ExportRun.task_run_id == context.task_run.id)
                ).scalar_one_or_none()
                if existing is None:
                    raise
                export_run = existing
            else:
                db.refresh(export_run)

            return (
                f"export run created: export_run_id={export_run.id} "
                f"row_count={export_run.row_count} "
                f"excluded={export_run.excluded_row_count} "
                f"groups={export_run.duplicate_groups_materialized_count} "
                f"status={export_run.status}"
            )
        finally:
            db.close()

    @staticmethod
    def _load_groups(
        db: Session, organization_id: uuid.UUID, match_run_id: uuid.UUID
    ) -> list[GroupInput]:
        """Reconstruct full group membership from MatchGroup + MatchDecision
        rows -- the exact reconstruction method Module 8's own audit
        endpoints already use (record_a_row_index/record_b_row_index for
        every decision referencing a group's id). Defensive: if the
        reconstructed member count doesn't match MatchGroup.record_count
        (possible only if MATCH_MAX_PERSISTED_DECISIONS capped that
        group's decisions), this fails permanently rather than silently
        exporting an incompletely-deduplicated file."""
        db_groups = db.execute(
            select(MatchGroup).where(
                MatchGroup.match_run_id == match_run_id,
                MatchGroup.organization_id == organization_id,
            )
        ).scalars().all()

        groups: list[GroupInput] = []
        for db_group in db_groups:
            decisions = db.execute(
                select(MatchDecision).where(
                    MatchDecision.match_group_id == db_group.id,
                    MatchDecision.organization_id == organization_id,
                )
            ).scalars().all()
            members: set[int] = {db_group.canonical_row_index}
            for decision in decisions:
                members.add(decision.record_a_row_index)
                members.add(decision.record_b_row_index)

            if len(members) != db_group.record_count:
                raise PermanentExecutionError(
                    "export cannot reconstruct full membership for match group "
                    f"{db_group.id} (expected {db_group.record_count} members, "
                    f"reconstructed {len(members)} from persisted match decisions "
                    "-- the group's decisions may have been capped by "
                    "MATCH_MAX_PERSISTED_DECISIONS)"
                )

            groups.append(
                GroupInput(
                    match_group_id=db_group.id,
                    canonical_row_index=db_group.canonical_row_index,
                    record_count=db_group.record_count,
                    member_row_indices=tuple(sorted(members)),
                )
            )
        return groups


def _serialize_csv(headers: list[str], rows: list[list[str]]) -> bytes:
    """Deterministic CSV serialization -- identical to
    app.worker.handlers.standardization._serialize_csv (small helper,
    intentionally copied rather than factored into a shared module, same
    reuse-by-copy precedent already established for this exact helper)."""
    buffer = StringIO()
    writer = csv_writer(buffer)
    writer.writerow(headers)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")
