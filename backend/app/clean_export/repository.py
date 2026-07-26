"""Module 19: ExportRepository.

All CleanExport database reads and writes. No business logic -- only
SELECT, INSERT, and atomic idempotency handling. The service layer
(ExportService) owns the orchestration.

Follows the catch-and-refetch idempotency pattern used by every prior
handler in this project (ExportHandler, QualityControlHandler, etc.):
  1. SELECT existing by idempotency_key before INSERT.
  2. INSERT new row.
  3. On IntegrityError (concurrent duplicate), rollback + SELECT again.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.clean_export.types import CleanExportResult, EligibilityResult
from app.models.clean_export import CleanExport


class ExportRepository:
    """CleanExport DB access: read, insert, idempotency."""

    def find_by_idempotency_key(
        self,
        db: Session,
        organization_id: uuid.UUID,
        idempotency_key: str,
    ) -> CleanExport | None:
        """Return an existing CleanExport for this (org, idempotency_key), or None."""
        return db.execute(
            select(CleanExport).where(
                CleanExport.organization_id == organization_id,
                CleanExport.idempotency_key == idempotency_key,
            )
        ).scalar_one_or_none()

    def create_blocked(
        self,
        db: Session,
        *,
        organization_id: uuid.UUID,
        job_id: uuid.UUID,
        data_source_id: uuid.UUID,
        format: str,
        idempotency_key: str,
        failure_reason: str,
        quality_control_run_id: uuid.UUID | None,
    ) -> CleanExport:
        """Persist a blocked CleanExport (eligibility check failed).

        Commits the row and returns the refreshed instance. Uses the same
        catch-and-refetch pattern as all prior handlers.
        """
        now = datetime.now(timezone.utc)
        export = CleanExport(
            id=uuid.uuid4(),
            organization_id=organization_id,
            job_id=job_id,
            data_source_id=data_source_id,
            dataset_version=quality_control_run_id,
            format=format,
            status="blocked",
            artifact_id=None,
            checksum=None,
            row_count=None,
            column_count=None,
            idempotency_key=idempotency_key,
            failure_reason=failure_reason,
            completed_at=now,
        )
        db.add(export)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = self.find_by_idempotency_key(db, organization_id, idempotency_key)
            if existing is None:
                raise
            return existing
        else:
            db.refresh(export)
        return export

    def create_failed(
        self,
        db: Session,
        *,
        organization_id: uuid.UUID,
        job_id: uuid.UUID,
        data_source_id: uuid.UUID,
        format: str,
        idempotency_key: str,
        failure_reason: str,
        quality_control_run_id: uuid.UUID | None,
    ) -> CleanExport:
        """Persist a failed CleanExport (export attempt encountered an error)."""
        now = datetime.now(timezone.utc)
        export = CleanExport(
            id=uuid.uuid4(),
            organization_id=organization_id,
            job_id=job_id,
            data_source_id=data_source_id,
            dataset_version=quality_control_run_id,
            format=format,
            status="failed",
            artifact_id=None,
            checksum=None,
            row_count=None,
            column_count=None,
            idempotency_key=idempotency_key,
            failure_reason=failure_reason,
            completed_at=now,
        )
        db.add(export)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = self.find_by_idempotency_key(db, organization_id, idempotency_key)
            if existing is None:
                raise
            return existing
        else:
            db.refresh(export)
        return export

    def create_completed(
        self,
        db: Session,
        *,
        organization_id: uuid.UUID,
        job_id: uuid.UUID,
        data_source_id: uuid.UUID,
        format: str,
        idempotency_key: str,
        result: CleanExportResult,
    ) -> CleanExport:
        """Persist a completed CleanExport with full artifact metadata."""
        export = CleanExport(
            id=uuid.uuid4(),
            organization_id=organization_id,
            job_id=job_id,
            data_source_id=data_source_id,
            dataset_version=result.quality_control_run_id,
            format=format,
            status="completed",
            artifact_id=result.artifact_id,
            checksum=result.checksum,
            row_count=result.row_count,
            column_count=result.column_count,
            idempotency_key=idempotency_key,
            failure_reason=None,
            completed_at=result.completed_at,
        )
        db.add(export)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            existing = self.find_by_idempotency_key(db, organization_id, idempotency_key)
            if existing is None:
                raise
            return existing
        else:
            db.refresh(export)
        return export

    def get_by_id(
        self,
        db: Session,
        export_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> CleanExport | None:
        """Return a CleanExport by id, scoped to organization_id."""
        return db.execute(
            select(CleanExport).where(
                CleanExport.id == export_id,
                CleanExport.organization_id == organization_id,
            )
        ).scalar_one_or_none()

    def list_by_job(
        self,
        db: Session,
        job_id: uuid.UUID,
        organization_id: uuid.UUID,
        limit: int = 50,
        offset: int = 0,
    ) -> list[CleanExport]:
        """Return all CleanExport rows for a given job, newest first."""
        return list(
            db.execute(
                select(CleanExport)
                .where(
                    CleanExport.job_id == job_id,
                    CleanExport.organization_id == organization_id,
                )
                .order_by(CleanExport.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
            .scalars()
            .all()
        )

    def count_by_job(
        self,
        db: Session,
        job_id: uuid.UUID,
        organization_id: uuid.UUID,
    ) -> int:
        """Return count of CleanExport rows for a given job."""
        from sqlalchemy import func as sa_func
        result = db.execute(
            select(sa_func.count()).select_from(CleanExport).where(
                CleanExport.job_id == job_id,
                CleanExport.organization_id == organization_id,
            )
        ).scalar()
        return result or 0
