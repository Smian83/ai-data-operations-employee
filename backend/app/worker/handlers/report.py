"""Module 20 Phase 1: ReportHandler — the worker entry point for REPORT TaskRuns.

Algorithm (eight steps):

1. Validate prerequisites: data_source present on task.

2. Early-exit idempotency: if a ReportRun for this task_run_id already exists,
   return the existing summary without further reads or engine calls. Same
   catch-and-refetch pattern as every prior handler.

3. Resolve the pipeline anchor:
   a. If source_task_run_id is set: look up that TaskRun's QualityControlRun
      (org-scoped). If none found, proceed with quality_control_run=None
      (partial report).
   b. If source_task_run_id is None: find the latest QualityControlRun for
      (organization_id, data_source_id) ordered by created_at DESC.
      If none found, partial report.

4. From the QualityControlRun (if found), resolve all upstream IDs via the
   denormalized fields already on that row (data_profile_id,
   issue_detection_run_id, remediation_run_id, validation_run_id). Load each
   row in one query each. From ValidationRun, get applied_remediation_run_id.

5. Load supplementary rows: ExportRun (approved, for same data_source/task),
   CleanExport (latest completed for same data_source/task),
   RemediationChangeDecision counts (GROUP BY), failed TaskRuns for the task.

6. Load org + task metadata needed for the report header.

7. Build ReportInput and call build_report() -- pure, no I/O.

8. Persist ReportRun in exactly ONE transaction. IntegrityError catch-and-refetch
   handles concurrent-duplicate-worker race.

Security constraints:
  - organization_id scoped on every query.
  - Cross-org source_task_run_id is indistinguishable from missing.
  - No writes outside report_runs.
  - No mutation of any upstream row.
  - No CSV file reads.
"""
from __future__ import annotations

import logging
import uuid
from collections.abc import Callable

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.db.session import SessionLocal
from app.models.clean_export import CleanExport
from app.models.data_profile import DataProfile
from app.models.enums import REPORT_ENGINE_VERSION, REPORT_SCHEMA_VERSION
from app.models.export_run import ExportRun
from app.models.issue_detection_run import IssueDetectionRun
from app.models.organization import Organization
from app.models.quality_control_run import QualityControlRun
from app.models.remediation_change_decision import RemediationChangeDecision
from app.models.remediation_run import RemediationRun
from app.models.report_run import ReportRun
from app.models.applied_remediation_run import AppliedRemediationRun
from app.models.task import Task
from app.models.task_run import TaskRun
from app.models.validation_run import ValidationRun
from app.reports.engine import ReportInput, build_report
from app.worker.handlers.base import ExecutionContext, PermanentExecutionError

logger = logging.getLogger(__name__)


class ReportHandler:
    """Generate and persist an immutable pipeline summary report for a
    REPORT TaskRun.

    One ReportRun row per REPORT TaskRun. Idempotency is enforced by
    UNIQUE(task_run_id) on report_runs plus the IntegrityError catch-and-refetch
    pattern every prior handler uses.
    """

    def __init__(self, session_factory: Callable[[], Session] = SessionLocal) -> None:
        self._session_factory = session_factory

    def execute(self, context: ExecutionContext) -> str:
        data_source = context.data_source
        if data_source is None:
            raise PermanentExecutionError(
                "report requires a data source on the Task"
            )

        organization_id = context.task_run.organization_id
        task_run_id = context.task_run.id
        task_id = context.task.id
        data_source_id = data_source.id

        db = self._session_factory()
        try:
            # ── Step 2: Early-exit idempotency ─────────────────────────────
            existing = db.execute(
                select(ReportRun).where(
                    ReportRun.task_run_id == task_run_id,
                )
            ).scalar_one_or_none()
            if existing is not None:
                return (
                    f"report run already exists: "
                    f"report_run_id={existing.id} "
                    f"qc_run_id={existing.quality_control_run_id}"
                )

            # ── Step 3: Resolve the pipeline anchor ────────────────────────
            quality_control_run: QualityControlRun | None = None
            source_task_run_id = context.task_run.source_task_run_id

            if source_task_run_id is not None:
                # Try to find a QualityControlRun for the given source run,
                # org-scoped. Cross-org IDs are treated as missing (404 behavior).
                quality_control_run = db.execute(
                    select(QualityControlRun).where(
                        QualityControlRun.task_run_id == source_task_run_id,
                        QualityControlRun.organization_id == organization_id,
                    )
                ).scalar_one_or_none()
                # If source_task_run_id points to a non-QC run, quality_control_run
                # will be None → proceed as a partial report.

            if quality_control_run is None:
                # Fallback: latest QC run for this data source (org-scoped).
                quality_control_run = db.execute(
                    select(QualityControlRun)
                    .where(
                        QualityControlRun.data_source_id == data_source_id,
                        QualityControlRun.organization_id == organization_id,
                    )
                    .order_by(QualityControlRun.created_at.desc())
                    .limit(1)
                ).scalar_one_or_none()

            # ── Step 4: Resolve upstream rows from QC chain ────────────────
            data_profile: DataProfile | None = None
            issue_detection_run: IssueDetectionRun | None = None
            remediation_run: RemediationRun | None = None
            applied_remediation_run: AppliedRemediationRun | None = None
            validation_run: ValidationRun | None = None

            # Corresponding TaskRuns for duration computation.
            sync_task_run: TaskRun | None = None
            detect_task_run: TaskRun | None = None
            remediate_task_run: TaskRun | None = None
            apply_task_run: TaskRun | None = None
            validate_task_run: TaskRun | None = None
            quality_ctrl_task_run: TaskRun | None = None

            if quality_control_run is not None:
                # Resolve ValidationRun.
                validation_run = db.execute(
                    select(ValidationRun).where(
                        ValidationRun.id == quality_control_run.validation_run_id,
                        ValidationRun.organization_id == organization_id,
                    )
                ).scalar_one_or_none()

                # Resolve RemediationRun.
                remediation_run = db.execute(
                    select(RemediationRun).where(
                        RemediationRun.id == quality_control_run.remediation_run_id,
                        RemediationRun.organization_id == organization_id,
                    )
                ).scalar_one_or_none()

                # Resolve IssueDetectionRun.
                issue_detection_run = db.execute(
                    select(IssueDetectionRun).where(
                        IssueDetectionRun.id == quality_control_run.issue_detection_run_id,
                        IssueDetectionRun.organization_id == organization_id,
                    )
                ).scalar_one_or_none()

                # Resolve DataProfile.
                data_profile = db.execute(
                    select(DataProfile).where(
                        DataProfile.id == quality_control_run.data_profile_id,
                        DataProfile.organization_id == organization_id,
                    )
                ).scalar_one_or_none()

                # Resolve AppliedRemediationRun from ValidationRun.
                if (
                    validation_run is not None
                    and validation_run.applied_remediation_run_id is not None
                ):
                    applied_remediation_run = db.execute(
                        select(AppliedRemediationRun).where(
                            AppliedRemediationRun.id
                            == validation_run.applied_remediation_run_id,
                            AppliedRemediationRun.organization_id == organization_id,
                        )
                    ).scalar_one_or_none()

                # Resolve stage TaskRuns for duration calculation.
                quality_ctrl_task_run = db.execute(
                    select(TaskRun).where(
                        TaskRun.id == quality_control_run.task_run_id,
                        TaskRun.organization_id == organization_id,
                    )
                ).scalar_one_or_none()

                if validation_run is not None:
                    validate_task_run = db.execute(
                        select(TaskRun).where(
                            TaskRun.id == validation_run.task_run_id,
                            TaskRun.organization_id == organization_id,
                        )
                    ).scalar_one_or_none()

                if applied_remediation_run is not None:
                    apply_task_run = db.execute(
                        select(TaskRun).where(
                            TaskRun.id == applied_remediation_run.task_run_id,
                            TaskRun.organization_id == organization_id,
                        )
                    ).scalar_one_or_none()

                if remediation_run is not None:
                    remediate_task_run = db.execute(
                        select(TaskRun).where(
                            TaskRun.id == remediation_run.task_run_id,
                            TaskRun.organization_id == organization_id,
                        )
                    ).scalar_one_or_none()

                if issue_detection_run is not None:
                    detect_task_run = db.execute(
                        select(TaskRun).where(
                            TaskRun.id == issue_detection_run.task_run_id,
                            TaskRun.organization_id == organization_id,
                        )
                    ).scalar_one_or_none()

                if data_profile is not None:
                    sync_task_run = db.execute(
                        select(TaskRun).where(
                            TaskRun.id == data_profile.task_run_id,
                            TaskRun.organization_id == organization_id,
                        )
                    ).scalar_one_or_none()

            else:
                # Partial pipeline: try to get DataProfile directly.
                data_profile = db.execute(
                    select(DataProfile)
                    .where(
                        DataProfile.data_source_id == data_source_id,
                        DataProfile.organization_id == organization_id,
                    )
                    .order_by(DataProfile.task_run_id.desc())
                    .limit(1)
                ).scalar_one_or_none()

                if data_profile is not None:
                    sync_task_run = db.execute(
                        select(TaskRun).where(
                            TaskRun.id == data_profile.task_run_id,
                            TaskRun.organization_id == organization_id,
                        )
                    ).scalar_one_or_none()

            # ── Step 5: Supplementary rows ──────────────────────────────────

            # Latest approved ExportRun for this data_source/task.
            export_run: ExportRun | None = db.execute(
                select(ExportRun)
                .where(
                    ExportRun.data_source_id == data_source_id,
                    ExportRun.organization_id == organization_id,
                    ExportRun.status == "approved",
                )
                .order_by(ExportRun.created_at.desc())
                .limit(1)
            ).scalar_one_or_none()

            # CleanExport — must be anchored to the resolved pipeline chain.
            # CleanExport.dataset_version == QualityControlRun.id is the FK that
            # ties a clean export to exactly the QC run that authorized it. Using
            # "latest by data_source" would allow a second pipeline execution's
            # export to appear in this report's audit_lineage (wrong pipeline chain).
            if quality_control_run is not None:
                clean_export: CleanExport | None = db.execute(
                    select(CleanExport)
                    .where(
                        CleanExport.dataset_version == quality_control_run.id,
                        CleanExport.organization_id == organization_id,
                        CleanExport.status == "completed",
                    )
                    .order_by(CleanExport.created_at.desc())
                    .limit(1)
                ).scalar_one_or_none()
            else:
                # Partial pipeline (no QC run) — best-effort latest completed export
                # for this data source.
                clean_export = db.execute(
                    select(CleanExport)
                    .where(
                        CleanExport.data_source_id == data_source_id,
                        CleanExport.organization_id == organization_id,
                        CleanExport.status == "completed",
                    )
                    .order_by(CleanExport.created_at.desc())
                    .limit(1)
                ).scalar_one_or_none()

            # CleanExport task_run is not directly recoverable without extra
            # queries; duration is omitted for clean_export stage in partial
            # pipelines (clean_export_task_run stays None unless we locate it).
            clean_export_task_run: TaskRun | None = None

            # RemediationChangeDecision counts (GROUP BY decision).
            approved_decisions = 0
            rejected_decisions = 0
            pending_decisions = 0
            if remediation_run is not None:
                decision_counts = db.execute(
                    select(
                        RemediationChangeDecision.decision,
                        func.count().label("cnt"),
                    )
                    .where(
                        RemediationChangeDecision.remediation_run_id
                        == remediation_run.id,
                        RemediationChangeDecision.organization_id == organization_id,
                    )
                    .group_by(RemediationChangeDecision.decision)
                ).all()
                for decision, cnt in decision_counts:
                    if decision == "approved":
                        approved_decisions = cnt
                    elif decision == "rejected":
                        rejected_decisions = cnt

                # pending = total_changes - approved - rejected
                total_changes = getattr(remediation_run, "total_changes_count", 0) or 0
                pending_decisions = max(
                    0, total_changes - approved_decisions - rejected_decisions
                )

            # Failed TaskRuns for this task (org-scoped), newest first.
            failed_task_runs = (
                db.execute(
                    select(TaskRun)
                    .where(
                        TaskRun.task_id == task_id,
                        TaskRun.organization_id == organization_id,
                        TaskRun.status == "failed",
                    )
                    .order_by(TaskRun.created_at.desc())
                    .limit(20)
                )
                .scalars()
                .all()
            )

            # ── Step 6: Org + task metadata ────────────────────────────────
            org = db.execute(
                select(Organization).where(Organization.id == organization_id)
            ).scalar_one_or_none()
            organization_name = org.name if org is not None else str(organization_id)

            task = db.execute(
                select(Task).where(
                    Task.id == task_id,
                    Task.organization_id == organization_id,
                )
            ).scalar_one_or_none()
            task_name = task.name if task is not None else str(task_id)

            # User identity for "generated_by".
            # "system" is the truthful value when no authenticated user is present
            # (worker-executed runs have no session user). Storing organization_id
            # here would be misleading — an org UUID is not a human identity.
            generated_by = "system"
            if hasattr(context, "user") and context.user is not None:
                generated_by = getattr(context.user, "email", "system")

            # ── Step 7: Build report data via pure engine ──────────────────
            report_input = ReportInput(
                organization_id=organization_id,
                organization_name=organization_name,
                task_id=task_id,
                task_name=task_name,
                data_source_id=data_source_id,
                data_source_name=data_source.name,
                generated_by=generated_by,
                sync_task_run=sync_task_run,
                detect_task_run=detect_task_run,
                remediate_task_run=remediate_task_run,
                apply_task_run=apply_task_run,
                validate_task_run=validate_task_run,
                quality_ctrl_task_run=quality_ctrl_task_run,
                clean_export_task_run=clean_export_task_run,
                data_profile=data_profile,
                issue_detection_run=issue_detection_run,
                remediation_run=remediation_run,
                applied_remediation_run=applied_remediation_run,
                validation_run=validation_run,
                quality_control_run=quality_control_run,
                clean_export=clean_export,
                export_run=export_run,
                approved_decision_count=approved_decisions,
                rejected_decision_count=rejected_decisions,
                pending_decision_count=pending_decisions,
                failed_task_runs=list(failed_task_runs),
            )
            report_data = build_report(report_input)

            # ── Step 8: Persist in exactly ONE transaction ─────────────────
            report_run = ReportRun(
                id=uuid.uuid4(),
                organization_id=organization_id,
                task_run_id=task_run_id,
                task_id=task_id,
                data_source_id=data_source_id,
                quality_control_run_id=(
                    quality_control_run.id
                    if quality_control_run is not None else None
                ),
                report_engine_version=REPORT_ENGINE_VERSION,
                report_schema_version=REPORT_SCHEMA_VERSION,
                report_data=report_data,
            )
            db.add(report_run)

            try:
                db.commit()
            except IntegrityError:
                db.rollback()
                # Catch-and-refetch: concurrent worker already persisted a
                # ReportRun for this task_run_id.
                report_run = db.execute(
                    select(ReportRun).where(
                        ReportRun.task_run_id == task_run_id,
                    )
                ).scalar_one_or_none()
                if report_run is None:
                    raise
            else:
                db.refresh(report_run)

            pipeline_status = report_data.get("job_summary", {}).get(
                "overall_status", "unknown"
            )
            qc_id = report_run.quality_control_run_id
            return (
                f"report run created: "
                f"report_run_id={report_run.id} "
                f"pipeline_status={pipeline_status} "
                f"qc_run_id={qc_id}"
            )
        finally:
            db.close()
