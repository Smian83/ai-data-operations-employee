"""Module 19: ExportEligibilityChecker.

Verifies that a dataset is eligible for clean export. A dataset is eligible
when ALL of the following hold:

  1. The Task (job_id) exists, is active, and belongs to this organization.
  2. An ExportRun with status='approved' exists for this task.
  3. A QualityControlRun with release_recommendation IN
     ('PASS', 'PASS_WITH_WARNINGS') exists for the same data_source.
     If dataset_version is supplied, the QualityControlRun's id must match.
  4. (Gate 4 -- APPLY_REMEDIATIONS): If an AppliedRemediationRun exists for
     this data_source, it must have a non-null output_file_path AND its
     decisions_snapshot_hash must match the hash of the current approved
     RemediationChangeDecision set. If the hash mismatches, the export is
     blocked (the artifact is stale -- re-run APPLY_REMEDIATIONS first).
     If no AppliedRemediationRun exists, Gate 4 is bypassed (legacy path).

This class is deliberately NOT pure (it takes a database session) but has
NO side effects -- it only issues SELECT queries, never writes anything.
Returns an EligibilityResult describing whether the export may proceed and,
if so, which artifact to load.
"""
from __future__ import annotations

import hashlib
import uuid
from datetime import timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.clean_export.types import EligibilityResult
from app.models.applied_remediation_run import AppliedRemediationRun
from app.models.export_run import ExportRun
from app.models.quality_control_run import QualityControlRun
from app.models.remediation_change import RemediationChange
from app.models.remediation_change_decision import RemediationChangeDecision
from app.models.remediation_run import RemediationRun
from app.models.task import Task


# QC recommendations that allow a clean export to proceed.
_PASS_RECOMMENDATIONS: frozenset[str] = frozenset(
    {"PASS", "PASS_WITH_WARNINGS"}
)


class ExportEligibilityChecker:
    """Check if a dataset is eligible for clean export.

    Instantiate once and call check() per request. No state is stored
    between calls -- this class is effectively a namespace for the
    eligibility logic with its single dependency (the DB session factory
    or session) made explicit.
    """

    def check(
        self,
        db: Session,
        organization_id: uuid.UUID,
        job_id: uuid.UUID,
        dataset_version: uuid.UUID | None,
    ) -> EligibilityResult:
        """Run all eligibility gates in order, returning on the first failure.

        Organization scoping is applied to every query so cross-tenant
        job_id values are indistinguishable from missing.
        """
        # Gate 1: Task must exist and be active.
        task = db.execute(
            select(Task).where(
                Task.id == job_id,
                Task.organization_id == organization_id,
                Task.is_active.is_(True),
            )
        ).scalar_one_or_none()
        if task is None:
            return EligibilityResult(
                eligible=False,
                export_run_id=None,
                export_run_artifact_path=None,
                quality_control_run_id=None,
                failure_reason=(
                    f"Task (job_id={job_id}) not found or inactive for "
                    f"organization_id={organization_id}"
                ),
            )

        data_source_id = task.data_source_id
        if data_source_id is None:
            return EligibilityResult(
                eligible=False,
                export_run_id=None,
                export_run_artifact_path=None,
                quality_control_run_id=None,
                failure_reason=(
                    f"Task (job_id={job_id}) has no associated data_source; "
                    "cannot locate an approved ExportRun"
                ),
            )

        # Gate 2: An approved ExportRun must exist for this task.
        export_run = db.execute(
            select(ExportRun).where(
                ExportRun.task_id == job_id,
                ExportRun.organization_id == organization_id,
                ExportRun.status == "approved",
            )
            # If multiple approved runs exist (e.g., after a re-export +
            # re-approval), take the most recently created one.
            .order_by(ExportRun.export_timestamp.desc())
            .limit(1)
        ).scalar_one_or_none()
        if export_run is None:
            return EligibilityResult(
                eligible=False,
                export_run_id=None,
                export_run_artifact_path=None,
                quality_control_run_id=None,
                failure_reason=(
                    f"No approved ExportRun found for job_id={job_id} "
                    f"(organization_id={organization_id}). "
                    "The Module 9 EXPORT task must be run and approved before "
                    "a clean export can be produced."
                ),
            )

        # Gate 3 (or Gate 3+4): A QualityControlRun with PASS/PASS_WITH_WARNINGS
        # must exist for the same data_source. If dataset_version is supplied,
        # it must match that specific QC run's id.
        if dataset_version is not None:
            qc_run = db.execute(
                select(QualityControlRun).where(
                    QualityControlRun.id == dataset_version,
                    QualityControlRun.organization_id == organization_id,
                    QualityControlRun.data_source_id == data_source_id,
                )
            ).scalar_one_or_none()
            if qc_run is None:
                return EligibilityResult(
                    eligible=False,
                    export_run_id=None,
                    export_run_artifact_path=None,
                    quality_control_run_id=None,
                    failure_reason=(
                        f"QualityControlRun dataset_version={dataset_version} "
                        f"not found for data_source_id={data_source_id} "
                        f"(organization_id={organization_id})"
                    ),
                )
            if qc_run.release_recommendation not in _PASS_RECOMMENDATIONS:
                return EligibilityResult(
                    eligible=False,
                    export_run_id=None,
                    export_run_artifact_path=None,
                    quality_control_run_id=qc_run.id,
                    failure_reason=(
                        f"QualityControlRun dataset_version={dataset_version} "
                        f"has release_recommendation={qc_run.release_recommendation!r} "
                        "(must be PASS or PASS_WITH_WARNINGS to allow a clean export)"
                    ),
                )
        else:
            # Find the latest PASS/PASS_WITH_WARNINGS QC run for this data source.
            qc_run = db.execute(
                select(QualityControlRun).where(
                    QualityControlRun.organization_id == organization_id,
                    QualityControlRun.data_source_id == data_source_id,
                    QualityControlRun.release_recommendation.in_(
                        list(_PASS_RECOMMENDATIONS)
                    ),
                )
                .order_by(QualityControlRun.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()
            if qc_run is None:
                return EligibilityResult(
                    eligible=False,
                    export_run_id=None,
                    export_run_artifact_path=None,
                    quality_control_run_id=None,
                    failure_reason=(
                        f"No QualityControlRun with recommendation PASS or "
                        f"PASS_WITH_WARNINGS found for data_source_id={data_source_id} "
                        f"(organization_id={organization_id}). "
                        "Module 18 quality control must be run and pass before "
                        "a clean export can be produced."
                    ),
                )

        # Gate 4: APPLY_REMEDIATIONS artifact check.
        # Find the most recent AppliedRemediationRun for this data_source,
        # scoped to this organization. If one exists, verify its
        # decisions_snapshot_hash still matches the current approved-decision
        # set. A mismatch means decisions changed after materialization -- the
        # artifact is stale and must not be exported.
        applied_run = db.execute(
            select(AppliedRemediationRun).where(
                AppliedRemediationRun.organization_id == organization_id,
                AppliedRemediationRun.data_source_id == data_source_id,
            )
            .order_by(AppliedRemediationRun.created_at.desc())
            .limit(1)
        ).scalar_one_or_none()

        if applied_run is not None:
            # Recompute the current approved-decision snapshot hash and compare.
            current_hash = _compute_current_decisions_hash(db, organization_id, applied_run)
            if current_hash != applied_run.decisions_snapshot_hash:
                return EligibilityResult(
                    eligible=False,
                    export_run_id=export_run.id,
                    export_run_artifact_path=export_run.output_file_path,
                    quality_control_run_id=qc_run.id,
                    failure_reason=(
                        "The APPLY_REMEDIATIONS artifact is stale: the approved "
                        "RemediationChangeDecision set has changed since the artifact "
                        "was materialized. Re-run APPLY_REMEDIATIONS to produce a "
                        "fresh artifact before exporting."
                    ),
                    applied_remediation_run_id=applied_run.id,
                )

            return EligibilityResult(
                eligible=True,
                export_run_id=export_run.id,
                export_run_artifact_path=export_run.output_file_path,
                quality_control_run_id=qc_run.id,
                failure_reason=None,
                applied_remediation_run_id=applied_run.id,
                applied_remediation_run_artifact_path=applied_run.output_file_path,
                applied_remediation_run_sha256=applied_run.output_sha256,
            )

        # No AppliedRemediationRun found -- legacy path (Gate 4 bypassed).
        return EligibilityResult(
            eligible=True,
            export_run_id=export_run.id,
            export_run_artifact_path=export_run.output_file_path,
            quality_control_run_id=qc_run.id,
            failure_reason=None,
        )


def _compute_current_decisions_hash(
    db: Session,
    organization_id: uuid.UUID,
    applied_run: AppliedRemediationRun,
) -> str:
    """Recompute the decisions_snapshot_hash for the current approved decision
    set linked to applied_run.remediation_run_id. Uses the same deterministic
    algorithm as ApprovedChangesApplicatorHandler._compute_decisions_snapshot_hash.
    """
    # Load all RemediationChange rows for this remediation_run.
    change_rows = (
        db.execute(
            select(RemediationChange).where(
                RemediationChange.remediation_run_id == applied_run.remediation_run_id,
                RemediationChange.organization_id == organization_id,
            )
        )
        .scalars()
        .all()
    )

    change_ids = [row.id for row in change_rows]
    if not change_ids:
        # No changes at all -- hash of empty set.
        return hashlib.sha256(b"").hexdigest()

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

    # Latest-row-wins per change_id (same as ApprovedChangesApplicatorHandler).
    import uuid as _uuid
    latest: dict[_uuid.UUID, RemediationChangeDecision] = {}
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
        if dt_new > dt_prev or (dt_new == dt_prev and str(decision.id) > str(prev.id)):
            latest[cid] = decision

    # Filter to approved only, then keep the RemediationChange rows.
    approved_changes = [
        row
        for row in change_rows
        if latest.get(row.id) is not None and latest[row.id].decision == "approved"
    ]

    # Same canonical hash as ApprovedChangesApplicatorHandler._compute_decisions_snapshot_hash.
    tuples = sorted(
        (str(change.id), change.action, change.row_number)
        for change in approved_changes
    )
    payload = "\n".join(f"{t[0]}|{t[1]}|{t[2]}" for t in tuples)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
