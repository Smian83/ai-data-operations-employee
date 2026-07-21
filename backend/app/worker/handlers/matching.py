"""Module 8 data matching & deduplication execution handler. See
docs/module-8-data-matching-deduplication-design.md for the full design.

Unlike CleaningHandler/StandardizationHandler, this handler writes NO
output file -- Module 8's deliverable is the audit data itself (MatchRun/
MatchGroup/MatchDecision/MatchSkippedBlock rows), never a rewritten CSV
(see the design doc Section 2's architectural decision). Matching never
opens the Module 7 standardized output for writing, and never performs
any physical merge or deletion of a duplicate record.
"""
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
from app.models.match_decision import MatchDecision
from app.models.match_group import MatchGroup
from app.models.match_rule_field import MatchRuleField
from app.models.match_rule_set import MatchRuleSet
from app.models.match_run import MatchRun
from app.models.match_skipped_block import MatchSkippedBlock
from app.models.standardization_run import StandardizationRun
from app.profiling.csv_loader import CsvLoadError, load_csv
from app.profiling.types import CsvLimits
from app.matching.engine import MATCH_ENGINE_VERSION, match
from app.matching.types import MatchLimits, MatchRuleFieldSpec, MatchRuleSetConfig
from app.worker.handlers.base import ExecutionContext, PermanentExecutionError


class MatchHandler:
    """Match/deduplicate the output of an APPROVED Module 7
    StandardizationRun, and persist one immutable MatchRun (+ bounded
    MatchGroup/MatchDecision/MatchSkippedBlock rows) per MATCH TaskRun.

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
            raise PermanentExecutionError("matching requires a data source")
        if data_source.source_type != SourceType.CSV_UPLOAD:
            raise PermanentExecutionError(
                f"MATCH is not implemented for source_type={data_source.source_type.value}"
            )

        source_task_run_id = context.task_run.source_task_run_id
        if source_task_run_id is None:
            raise PermanentExecutionError("matching requires source_task_run_id on the TaskRun")

        db = self._session_factory()
        try:
            standardization_run = db.execute(
                select(StandardizationRun).where(
                    StandardizationRun.task_run_id == source_task_run_id,
                    StandardizationRun.organization_id == context.task_run.organization_id,
                )
            ).scalar_one_or_none()
            if standardization_run is None:
                raise PermanentExecutionError(
                    "matching requires a completed standardization run for source_task_run_id"
                )
            if standardization_run.status != "approved":
                raise PermanentExecutionError(
                    "matching requires an APPROVED standardization run for "
                    f"source_task_run_id (current status: {standardization_run.status})"
                )

            existing = db.execute(
                select(MatchRun).where(MatchRun.task_run_id == context.task_run.id)
            ).scalar_one_or_none()
            if existing is not None:
                return (
                    f"match run already exists: match_run_id={existing.id} "
                    f"groups={existing.duplicate_group_count} status={existing.status}"
                )

            settings = get_settings()
            csv_limits = self._csv_limits(settings)
            try:
                # standardization_run.output_file_path is never client-
                # supplied -- it was computed and written entirely
                # server-side by StandardizationHandler, already scoped
                # to CSV_STANDARDIZED_ROOT/{organization_id}/ at write
                # time, and standardization_run itself was just looked
                # up scoped to this exact organization_id above. No
                # additional path-escape validation is needed, same
                # reasoning StandardizationHandler already documents for
                # cleaning_run.output_file_path.
                loaded = load_csv(Path(standardization_run.output_file_path), csv_limits)
            except CsvLoadError as exc:
                raise PermanentExecutionError(str(exc)) from exc

            rule_set_row, rule_set_config = self._load_rule_set(
                db, context.task_run.organization_id, data_source.id
            )
            limits = MatchLimits(
                max_block_size=settings.match_max_block_size,
                max_persisted_decisions=settings.match_max_persisted_decisions,
                max_skipped_row_sample=settings.match_max_skipped_row_sample,
            )

            result = match(loaded.rows, loaded.headers, rule_set_config, limits)

            match_run = MatchRun(
                id=uuid.uuid4(),
                organization_id=context.task_run.organization_id,
                task_run_id=context.task_run.id,
                task_id=context.task.id,
                data_source_id=data_source.id,
                source_task_run_id=source_task_run_id,
                rule_set_id=rule_set_row.id if rule_set_row is not None else None,
                rule_set_version=rule_set_row.version if rule_set_row is not None else None,
                row_count=result.row_count,
                total_comparisons_count=result.total_comparisons_count,
                duplicate_group_count=result.duplicate_group_count,
                duplicate_pairs_count=result.duplicate_pairs_count,
                ambiguous_pairs_count=result.ambiguous_pairs_count,
                skipped_block_count=result.skipped_block_count,
                decisions_by_rule=result.decisions_by_rule,
                confidence_score=result.confidence_score,
                match_engine_version=MATCH_ENGINE_VERSION,
                status="pending_review",
            )
            db.add(match_run)

            group_models: list[MatchGroup] = []
            for group in result.groups:
                group_model = MatchGroup(
                    id=uuid.uuid4(),
                    organization_id=context.task_run.organization_id,
                    match_run_id=match_run.id,
                    canonical_row_index=group.canonical_row_index,
                    record_count=group.record_count,
                    confidence_score=group.confidence_score,
                )
                db.add(group_model)
                group_models.append(group_model)

            for decision in result.decisions:
                match_group_id = (
                    group_models[decision.group_index].id
                    if decision.group_index is not None
                    else None
                )
                db.add(
                    MatchDecision(
                        organization_id=context.task_run.organization_id,
                        match_run_id=match_run.id,
                        match_group_id=match_group_id,
                        record_a_row_index=decision.record_a_row_index,
                        record_b_row_index=decision.record_b_row_index,
                        blocking_key=decision.blocking_key,
                        rule_name=decision.rule_name,
                        field_comparisons=decision.field_comparisons,
                        total_score=decision.total_score,
                        threshold_used=decision.threshold_used,
                        decision=decision.decision,
                        confidence_score=decision.confidence_score,
                        reason=decision.reason,
                        rule_version=decision.rule_version,
                    )
                )

            for skipped in result.skipped_blocks:
                db.add(
                    MatchSkippedBlock(
                        organization_id=context.task_run.organization_id,
                        match_run_id=match_run.id,
                        blocking_key=skipped.blocking_key,
                        block_size=skipped.block_size,
                        sample_row_indices=list(skipped.sample_row_indices),
                    )
                )

            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = db.execute(
                    select(MatchRun).where(MatchRun.task_run_id == context.task_run.id)
                ).scalar_one_or_none()
                if existing is None:
                    raise
                match_run = existing
            else:
                db.refresh(match_run)

            return (
                f"match run created: match_run_id={match_run.id} "
                f"groups={match_run.duplicate_group_count} "
                f"duplicates={match_run.duplicate_pairs_count} "
                f"ambiguous={match_run.ambiguous_pairs_count} "
                f"confidence={match_run.confidence_score:.2f} status={match_run.status}"
            )
        finally:
            db.close()

    @staticmethod
    def _load_rule_set(
        db: Session, organization_id: uuid.UUID, data_source_id: uuid.UUID
    ) -> tuple[MatchRuleSet | None, MatchRuleSetConfig | None]:
        """Data-source-specific active rule set takes precedence over an
        org-wide (data_source_id IS NULL) one -- same precedence
        StandardizationHandler._load_column_overrides already applies. If
        neither exists, returns (None, None): only the always-available
        Stage-1 exact-duplicate pass runs (Section 4/7) -- a safe, valid,
        complete outcome, never a forced default rule set."""
        rows = db.execute(
            select(MatchRuleSet).where(
                MatchRuleSet.organization_id == organization_id,
                MatchRuleSet.is_active.is_(True),
            )
        ).scalars().all()

        scoped = next((r for r in rows if r.data_source_id == data_source_id), None)
        org_wide = next((r for r in rows if r.data_source_id is None), None)
        rule_set_row = scoped or org_wide
        if rule_set_row is None:
            return None, None

        field_rows = db.execute(
            select(MatchRuleField)
            .where(MatchRuleField.rule_set_id == rule_set_row.id)
            .order_by(MatchRuleField.created_at, MatchRuleField.id)
        ).scalars().all()
        if not field_rows:
            return rule_set_row, None

        config = MatchRuleSetConfig(
            duplicate_threshold=rule_set_row.duplicate_threshold,
            review_threshold=rule_set_row.review_threshold,
            fields=tuple(
                MatchRuleFieldSpec(
                    column_name=f.column_name,
                    comparison_type=f.comparison_type,
                    weight=f.weight,
                )
                for f in field_rows
            ),
        )
        return rule_set_row, config
