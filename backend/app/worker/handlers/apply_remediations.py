"""APPLY_REMEDIATIONS execution handler.

Bridges Module 16 (approval decisions) and Module 17 (validation) by
materializing an immutable, post-remediation CSV artifact from the approved
Module 9 ExportRun artifact plus the approved Module 16
RemediationChangeDecision rows.

Algorithm (six steps, following the established handler pattern):

1. Resolve the upstream RemediationRun via source_task_run_id (scoped to
   organization_id). Missing -> permanent failure. Then walk from
   RemediationRun back to ExportRun by two hops:
     RemediationRun.source_task_run_id -> IssueDetectionRun (lineage check)
     detect TaskRun.source_task_run_id -> ExportRun
   In this pipeline DETECT (M14) runs after EXPORT (M9), so the detect
   TaskRun's source_task_run_id points directly at the EXPORT TaskRun.
   ExportRun must have status='approved' or we fail permanently.

2. Early-exit if an AppliedRemediationRun for this task_run_id already
   exists (same idempotency-first pattern as every prior handler).

3. Freeze the approved-change snapshot (same latest-decision-wins logic as
   ValidationHandler Step 3). No re-query after this point.

4. Load the ExportRun artifact CSV. Verify SHA-256 integrity (same pattern
   as Module 10's open_verified_artifact). Apply the approved changes to the
   in-memory dataset:
     - Cell-value changes (trim_whitespace, etc.): replace row[row_number]
       [column_name] with proposed_value.
     - Row-removal changes (remove_duplicate_row, remove_duplicate_primary_key):
       collect the row indices and filter them out after all cell changes.
   Row indices that are out-of-range for the ExportRun artifact's data rows,
   or that reference a column not present in the artifact headers, are
   SKIPPED (logged to skipped_change_count, never silent).

5. Serialize the modified dataset to UTF-8 CSV, compute SHA-256, write to
   csv_remediated_root/{organization_id}/{task_run_id}.csv.

6. Compute decisions_snapshot_hash (SHA-256 of sorted canonical
   representation of applied decision set). Persist AppliedRemediationRun
   in one short transaction. IntegrityError catch-and-refetch (same race-
   condition safety net as every prior handler).

Security constraints:
  - Organization-scoped on every query. The handler never reads a row from
    another organization.
  - ExportRun artifact is NEVER opened for writing (read-only).
  - No writes outside AppliedRemediationRun, plus the artifact file.
  - No mutation of any RemediationChange, RemediationChangeDecision,
    ExportRun, or any other upstream row.
"""
from __future__ import annotations

import csv
import hashlib
import io
import uuid
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.artifacts.download import (
    ArtifactIntegrityError,
    ArtifactMissingError,
    ArtifactPathError,
    open_verified_artifact,
    resolve_artifact_path,
)
from app.core.config import get_settings
from app.db.session import SessionLocal
from app.models.applied_remediation_run import AppliedRemediationRun
from app.models.enums import SourceType
from app.models.export_run import ExportRun
from app.models.issue_detection_run import IssueDetectionRun
from app.models.remediation_change import RemediationChange
from app.models.remediation_change_decision import RemediationChangeDecision
from app.models.remediation_run import RemediationRun
from app.models.task_run import TaskRun
from app.worker.handlers.base import ExecutionContext, PermanentExecutionError

APPLY_ENGINE_VERSION = "1.0.0"

# Row-removal actions that mark an entire row for exclusion rather than
# patching a single cell value. Same set as in the remediation engine.
_ROW_REMOVAL_ACTIONS = frozenset({
    "remove_duplicate_row",
    "remove_duplicate_primary_key",
})

# Actions for which the handler applies the semantic operation to the
# CURRENT cell value in the export artifact rather than the pre-computed
# proposed_value. This is necessary because proposed_value is computed by
# the remediation engine against the original raw source data, which may
# differ from the post-cleaning, post-standardization export artifact.
# For example, the cleaning step may have already stripped leading/trailing
# whitespace, so the export artifact cell contains "Alice" while proposed_value
# is "Alice " (lstrip of " Alice ") or " Alice" (rstrip). Applying
# proposed_value would make the already-clean cell worse.
#
# The semantic re-application maps action -> callable(current_value: str) -> str.
_SEMANTIC_ACTIONS: dict[str, Callable[[str], str]] = {
    "trim_whitespace": str.strip,
}


class ApprovedChangesApplicatorHandler:
    """Apply the approved RemediationChange proposals to the ExportRun
    artifact and persist one immutable AppliedRemediationRun per
    APPLY_REMEDIATIONS TaskRun.

    The output CSV is written to csv_remediated_root/{org}/{task_run_id}.csv.
    The ExportRun artifact is never opened for writing.

    Persistence uses the same short independent transaction and
    unique-constraint-plus-refetch idempotency pattern every prior handler
    already uses.
    """

    def __init__(self, session_factory: Callable[[], Session] = SessionLocal) -> None:
        self._session_factory = session_factory

    def execute(self, context: ExecutionContext) -> str:  # noqa: C901 -- deliberate: mirroring RemediationHandler's own step length
        data_source = context.data_source
        if data_source is None:
            raise PermanentExecutionError("apply_remediations requires a data source")
        if data_source.source_type != SourceType.CSV_UPLOAD:
            raise PermanentExecutionError(
                f"APPLY_REMEDIATIONS is not implemented for "
                f"source_type={data_source.source_type.value}"
            )

        source_task_run_id = context.task_run.source_task_run_id
        if source_task_run_id is None:
            raise PermanentExecutionError(
                "apply_remediations requires source_task_run_id on the TaskRun"
            )

        organization_id = context.task_run.organization_id

        db = self._session_factory()
        try:
            # ── Step 1: resolve upstream RemediationRun, then walk to ExportRun ──
            remediation_run = db.execute(
                select(RemediationRun).where(
                    RemediationRun.task_run_id == source_task_run_id,
                    RemediationRun.organization_id == organization_id,
                )
            ).scalar_one_or_none()
            if remediation_run is None:
                raise PermanentExecutionError(
                    "apply_remediations requires a completed remediation run for "
                    "source_task_run_id"
                )

            # Walk the chain: RemediationRun -> IssueDetectionRun -> ExportRun.
            #
            # In this pipeline, DETECT (M14) runs after EXPORT (M9).
            # The detect TaskRun's source_task_run_id points directly at the
            # EXPORT TaskRun, so we need only two hops:
            #   1. RemediationRun.source_task_run_id -> detect TaskRun ID
            #   2. detect TaskRun.source_task_run_id -> export TaskRun ID
            #   3. ExportRun.task_run_id == export TaskRun ID
            #
            # IssueDetectionRun has no source_task_run_id column (it is a
            # summary-only record). We load it purely for lineage verification.
            detection_run = db.execute(
                select(IssueDetectionRun).where(
                    IssueDetectionRun.task_run_id == remediation_run.source_task_run_id,
                    IssueDetectionRun.organization_id == organization_id,
                )
            ).scalar_one_or_none()
            if detection_run is None:
                raise PermanentExecutionError(
                    "apply_remediations cannot resolve the upstream IssueDetectionRun "
                    "from the RemediationRun's source_task_run_id"
                )

            # Fetch the detect TaskRun so we can read its source_task_run_id
            # (which is the EXPORT task run ID).
            detect_task_run = db.execute(
                select(TaskRun).where(
                    TaskRun.id == detection_run.task_run_id,
                )
            ).scalar_one_or_none()
            if detect_task_run is None or detect_task_run.source_task_run_id is None:
                raise PermanentExecutionError(
                    "apply_remediations cannot resolve the detect TaskRun or its "
                    "source_task_run_id (must point at the approved EXPORT TaskRun)"
                )

            export_run = db.execute(
                select(ExportRun).where(
                    ExportRun.task_run_id == detect_task_run.source_task_run_id,
                    ExportRun.organization_id == organization_id,
                )
            ).scalar_one_or_none()
            if export_run is None:
                raise PermanentExecutionError(
                    "apply_remediations cannot resolve the upstream ExportRun "
                    "from the detect TaskRun's source_task_run_id"
                )
            if export_run.status != "approved":
                raise PermanentExecutionError(
                    "apply_remediations requires an APPROVED ExportRun "
                    f"(current status: {export_run.status})"
                )

            # ── Step 2: idempotency short-circuit ────────────────────────────
            existing = db.execute(
                select(AppliedRemediationRun).where(
                    AppliedRemediationRun.task_run_id == context.task_run.id
                )
            ).scalar_one_or_none()
            if existing is not None:
                return (
                    f"applied remediation run already exists: "
                    f"applied_remediation_run_id={existing.id} "
                    f"applied_change_count={existing.applied_change_count} "
                    f"skipped_change_count={existing.skipped_change_count}"
                )

            # ── Step 3: freeze the approved-change snapshot ──────────────────
            # Mirrors ValidationHandler steps 3a-3c exactly.
            change_rows = (
                db.execute(
                    select(RemediationChange).where(
                        RemediationChange.remediation_run_id == remediation_run.id,
                        RemediationChange.organization_id == organization_id,
                    )
                )
                .scalars()
                .all()
            )

            change_ids = [row.id for row in change_rows]
            if change_ids:
                decision_rows = (
                    db.execute(
                        select(RemediationChangeDecision).where(
                            RemediationChangeDecision.remediation_change_id.in_(change_ids),
                            RemediationChangeDecision.organization_id == organization_id,
                        )
                    )
                    .scalars()
                    .all()
                )
            else:
                decision_rows = []

            # Latest-row-wins per change_id (identical to ValidationHandler).
            latest: dict[uuid.UUID, RemediationChangeDecision] = {}
            for decision in decision_rows:
                cid = decision.remediation_change_id
                prev = latest.get(cid)
                if prev is None:
                    latest[cid] = decision
                    continue
                dt_new = decision.decision_timestamp
                dt_prev = prev.decision_timestamp
                if dt_new.tzinfo is None:
                    dt_new = dt_new.replace(tzinfo=timezone.utc)
                if dt_prev.tzinfo is None:
                    dt_prev = dt_prev.replace(tzinfo=timezone.utc)
                if dt_new > dt_prev or (
                    dt_new == dt_prev and str(decision.id) > str(prev.id)
                ):
                    latest[cid] = decision

            # Keep only changes whose latest decision is "approved".
            approved_changes = [
                row
                for row in change_rows
                if latest.get(row.id) is not None
                and latest[row.id].decision == "approved"
            ]

            # ── Step 4: load ExportRun artifact, apply approved changes ──────
            settings = get_settings()
            tenant_root = (
                Path(settings.csv_exported_root) / str(organization_id)
            )

            try:
                resolved = resolve_artifact_path(tenant_root, export_run.output_file_path)
            except ArtifactPathError as exc:
                raise PermanentExecutionError(
                    f"ExportRun artifact path escapes tenant root: {exc}"
                ) from exc

            try:
                fileobj = open_verified_artifact(resolved, export_run.output_sha256)
            except ArtifactMissingError as exc:
                raise PermanentExecutionError(
                    f"ExportRun artifact not found or unreadable: {exc}"
                ) from exc
            except ArtifactIntegrityError as exc:
                raise PermanentExecutionError(
                    f"ExportRun artifact SHA-256 mismatch: {exc}"
                ) from exc

            try:
                raw_bytes = fileobj.read()
            finally:
                fileobj.close()

            try:
                text = raw_bytes.decode("utf-8")
                reader = csv.reader(io.StringIO(text))
                all_rows = list(reader)
            except (UnicodeDecodeError, csv.Error) as exc:
                raise PermanentExecutionError(
                    f"ExportRun artifact is not valid UTF-8 CSV: {exc}"
                ) from exc

            if not all_rows:
                raise PermanentExecutionError("ExportRun artifact is empty")

            headers = all_rows[0]
            # Work on mutable lists for patching.
            data_rows: list[list[str]] = [list(row) for row in all_rows[1:]]

            header_index: dict[str, int] = {h: i for i, h in enumerate(headers)}
            row_removal_indices: set[int] = set()
            applied_change_count = 0
            skipped_change_count = 0

            for change in approved_changes:
                # row_number follows the same 1-indexed CSV convention used by
                # the detection and remediation engines: header = row 1, first
                # data row = row 2 (i.e. _csv_row_number(idx) = idx + 2).
                # Convert to a 0-based index into data_rows.
                row_idx = change.row_number - 2
                if row_idx < 0 or row_idx >= len(data_rows):
                    skipped_change_count += 1
                    continue

                if change.action in _ROW_REMOVAL_ACTIONS:
                    row_removal_indices.add(row_idx)
                    applied_change_count += 1
                    continue

                # Cell-value patch.
                if change.column_name is None:
                    skipped_change_count += 1
                    continue
                col_idx = header_index.get(change.column_name)
                if col_idx is None:
                    skipped_change_count += 1
                    continue

                # For actions listed in _SEMANTIC_ACTIONS, apply the
                # operation to the current cell value (in the export artifact)
                # rather than the pre-computed proposed_value.  This prevents
                # the cleaning step's already-applied transformations from being
                # overwritten with a value derived from the original raw source.
                semantic_fn = _SEMANTIC_ACTIONS.get(change.action)
                if semantic_fn is not None:
                    new_value = semantic_fn(data_rows[row_idx][col_idx])
                else:
                    new_value = change.proposed_value if change.proposed_value is not None else ""
                data_rows[row_idx][col_idx] = new_value
                applied_change_count += 1

            # Filter out removed rows (stable-order, same as EXPORT engine).
            if row_removal_indices:
                data_rows = [
                    row
                    for i, row in enumerate(data_rows)
                    if i not in row_removal_indices
                ]

            # ── Step 5: serialize, compute SHA-256, write artifact ────────────
            output_bytes = _serialize_csv(headers, data_rows)
            output_sha256 = hashlib.sha256(output_bytes).hexdigest()

            output_dir = Path(settings.csv_remediated_root) / str(organization_id)
            output_dir.mkdir(parents=True, exist_ok=True)
            output_path = output_dir / f"{context.task_run.id}.csv"
            output_path.write_bytes(output_bytes)

            # ── Step 6: decisions_snapshot_hash, persist AppliedRemediationRun ──
            decisions_snapshot_hash = _compute_decisions_snapshot_hash(approved_changes)

            applied_run = AppliedRemediationRun(
                id=uuid.uuid4(),
                organization_id=organization_id,
                task_run_id=context.task_run.id,
                task_id=context.task.id,
                data_source_id=data_source.id,
                remediation_run_id=remediation_run.id,
                output_file_path=str(output_path),
                output_sha256=output_sha256,
                decisions_snapshot_hash=decisions_snapshot_hash,
                applied_change_count=applied_change_count,
                skipped_change_count=skipped_change_count,
                apply_engine_version=APPLY_ENGINE_VERSION,
            )
            db.add(applied_run)

            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                existing = db.execute(
                    select(AppliedRemediationRun).where(
                        AppliedRemediationRun.task_run_id == context.task_run.id
                    )
                ).scalar_one_or_none()
                if existing is None:
                    raise
                applied_run = existing
            else:
                db.refresh(applied_run)

            return (
                f"applied remediation run created: "
                f"applied_remediation_run_id={applied_run.id} "
                f"applied_change_count={applied_run.applied_change_count} "
                f"skipped_change_count={applied_run.skipped_change_count}"
            )
        finally:
            db.close()


def _serialize_csv(headers: list[str], rows: list[list[str]]) -> bytes:
    """Deterministic CSV serialization -- same helper as ExportHandler and
    StandardizationHandler, intentionally copied (same reuse-by-copy
    precedent established for this small helper across the project)."""
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    writer.writerows(rows)
    return buffer.getvalue().encode("utf-8")


def _compute_decisions_snapshot_hash(
    approved_changes: list[RemediationChange],
) -> str:
    """SHA-256 of the sorted, canonical representation of the applied decision
    set. Deterministic: sorted by (change_id_str, action, row_number) so the
    hash is identical for any ordering of approved_changes with the same
    content.

    Used by Module 19 Gate 4 to detect whether the decision set has changed
    after this artifact was materialized -- if the hash no longer matches the
    current approved decisions, Module 19 blocks rather than exporting a stale
    artifact.
    """
    # Build a sorted canonical list of (change_id, action, row_number) tuples.
    # change_id is a UUID -- str() gives a consistent canonical form.
    tuples = sorted(
        (str(change.id), change.action, change.row_number)
        for change in approved_changes
    )
    # Encode as newline-joined "change_id|action|row_number" strings.
    payload = "\n".join(
        f"{t[0]}|{t[1]}|{t[2]}" for t in tuples
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
