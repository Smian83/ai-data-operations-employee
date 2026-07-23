"""
Output artifact retention (Module 13).

Gives Settings.output_retention_* real, executable meaning: a bounded pass
that finds output artifacts (CleaningRun/StandardizationRun/ExportRun
output files) whose owning run reached a terminal, human-decided status
long enough ago, and deletes them -- never a source file under
CSV_INPUT_ROOT, never a run still pending_review, never anything an
operator has not explicitly opted into by setting
OUTPUT_RETENTION_ENABLED=true.

Four-state lifecycle (see docs/module-13-output-artifact-retention-design.md),
represented with NO new lifecycle-state column anywhere:

    ACTIVE   -- pending_review regardless of age, OR terminal but still
                inside the configured retention window. The default/
                common state; not queried for directly, just "everything
                the eligibility query below does not select."
    EXPIRED  -- terminal status, output_deleted_at IS NULL, and the
                decision timestamp matching the CURRENT status (never an
                older one -- see _decision_timestamp_expression) is at or
                before the configured cutoff. Purely derived by the
                eligibility query every pass; never stored.
    PURGE_PENDING -- represented only by the SELECT ... FOR UPDATE
                (SKIP LOCKED on PostgreSQL) row lock held for the
                duration of one artifact's own transaction, plus the
                'started'-equivalent moment inside that same
                transaction. No persisted claim/lease column -- see
                ArtifactRetentionEvent's own docstring for why.
    PURGED   -- output_deleted_at is set (real passes only -- see
                dry_run below). Terminal.

Transaction shape: one independent, committed transaction per artifact,
never one transaction for a whole batch -- directly reusing the pattern
app.worker.scheduler.run_due_schedules() already established and proved
under real PostgreSQL concurrency for Module 12. A failure processing one
artifact rolls back only that artifact's own transaction; every other
artifact already committed earlier in the same pass keeps its progress.

Concurrency safety uses the identical two-layer pattern already proven in
engine.py::claim_batch, reaper.py::reap_expired_runs, and
scheduler.py::run_due_schedules: a `SELECT ... FOR UPDATE SKIP LOCKED`
claim (degrading to a plain SELECT on SQLite -- see
app.worker.engine._supports_skip_locked), plus a guarded UPDATE whose
WHERE clause re-checks output_deleted_at IS NULL immediately before
persisting it. Under correct SKIP LOCKED usage this guard should never
actually lose a race (the row lock is held for the artifact's entire
transaction) -- it exists as defense-in-depth, exactly the same
"should be impossible... but the guard makes it safe regardless"
reasoning scheduler.py already documents for its own next_run_at guard.

Dry run: storage.exists() is called instead of storage.delete() (the one
place in this module exists() is used at all -- see
app.artifacts.storage.ArtifactStorage's own docstring on why a
check-then-delete pattern is otherwise avoided), output_deleted_at is
NEVER set regardless of outcome, and the pass may re-discover and
re-audit the same eligible artifact on a later pass (real or dry-run)
without limit, since nothing durable changed.
"""
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import case, func, select, update
from sqlalchemy.orm import Session

from app.artifacts.download import ArtifactPathError, resolve_artifact_path
from app.artifacts.storage import get_artifact_storage
from app.core.config import get_settings
from app.models.artifact_retention_event import ArtifactRetentionEvent
from app.models.cleaning_run import CleaningRun
from app.models.export_run import ExportRun
from app.models.standardization_run import StandardizationRun
from app.worker import metrics
from app.worker.engine import _supports_skip_locked

logger = logging.getLogger(__name__)

# The three terminal statuses eligible for retention at all -- never
# pending_review, which is excluded structurally by not appearing here,
# not by a separate check anywhere else.
_TERMINAL_STATUSES = ("approved", "rejected", "rolled_back")

# The failure-reason vocabulary this module produces
# ("unsafe_path"/"permission_denied"/"invalid_artifact_type"/
# "filesystem_error"/"database_conflict", returned as literal strings by
# _classify_os_error and _evaluate_and_act below) is the single source of
# truth in app.models.artifact_retention_event.
# ARTIFACT_RETENTION_FAILURE_REASON_CODES -- not re-declared here, to
# avoid a second, driftable copy in this file.


@dataclass(frozen=True)
class RetentionPassResult:
    """Typed counts for one purge_expired_artifacts() call. Every count
    below reflects a real, committed ArtifactRetentionEvent row except
    where noted -- candidates_considered is derived from the other counts
    (by construction, not tracked independently), so it can never drift
    out of sync with them."""

    purged_count: int  # real deletions: outcome=completed, dry_run=False
    already_missing_count: int  # outcome=already_missing (either dry_run mode)
    failed_count: int  # outcome=failed
    dry_run_would_purge_count: int  # outcome=completed, dry_run=True
    bytes_reclaimed: int  # sum of artifact_size_bytes, purged_count rows only
    dry_run: bool  # the mode this pass actually ran under

    @property
    def candidates_considered(self) -> int:
        return (
            self.purged_count
            + self.already_missing_count
            + self.failed_count
            + self.dry_run_would_purge_count
        )


@dataclass(frozen=True)
class _RunTypeConfig:
    """One entry per output-producing run table. Mirrors
    app.api.tasks._ARTIFACT_ROOT_SETTINGS / _ARTIFACT_RUN_ID_FIELDS'
    exact mapping (kept as a separate, worker-owned copy rather than a
    shared import, since app.worker must not depend on app.api)."""

    artifact_type: str
    model: type
    output_root_setting: str
    event_run_id_field: str


_RUN_TYPE_CONFIGS: tuple[_RunTypeConfig, ...] = (
    _RunTypeConfig("cleaning", CleaningRun, "csv_output_root", "cleaning_run_id"),
    _RunTypeConfig(
        "standardization", StandardizationRun, "csv_standardized_root", "standardization_run_id"
    ),
    _RunTypeConfig("export", ExportRun, "csv_exported_root", "export_run_id"),
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _decision_timestamp_expression(model: type):
    """The decision timestamp matching the run's CURRENT status --
    approved_at for status='approved', rejected_at for 'rejected',
    rolled_back_at for 'rolled_back' -- never an older timestamp from a
    prior transition (e.g. a rolled_back run always uses rolled_back_at,
    never its earlier approved_at, even though both columns are
    populated). NULL for any other status, which structurally excludes
    pending_review from ever matching the eligibility filter below."""
    return case(
        (model.status == "approved", model.approved_at),
        (model.status == "rejected", model.rejected_at),
        (model.status == "rolled_back", model.rolled_back_at),
        else_=None,
    )


def _compute_backlog_snapshot(db: Session, cutoff: datetime) -> tuple[int, float]:
    """Module 13 Phase 5 (metrics only): a read-only observability
    snapshot of the retention backlog -- total count of artifacts that
    currently match the eligibility criteria but have not yet been
    purged, and the age (in seconds) of the single oldest one, across all
    three run types combined, as of `_now()`. Returns (0, 0.0) when
    nothing is currently eligible.

    Deliberately plain COUNT()/MIN() aggregate reads: no
    `with_for_update()`, no row locking, no interaction whatsoever with
    the claim-based loop in _process_one_run_type above -- this cannot
    contend with, delay, or otherwise affect the real claim/delete
    transactions. Called once per enabled pass, AFTER that pass's own
    claim loop has already committed everything it purged this pass, so
    the snapshot reflects genuinely remaining backlog rather than a
    stale pre-pass count. Never called on purge_expired_artifacts()'s
    disabled early-return path -- see that function's docstring for why
    that path must open no query at all."""
    total = 0
    oldest_ts: datetime | None = None
    for config in _RUN_TYPE_CONFIGS:
        model = config.model
        decision_ts = _decision_timestamp_expression(model)
        count, min_ts = db.execute(
            select(func.count(), func.min(decision_ts)).where(
                model.status.in_(_TERMINAL_STATUSES),
                model.output_deleted_at.is_(None),
                decision_ts.is_not(None),
                decision_ts <= cutoff,
            )
        ).one()
        total += count
        if min_ts is not None and (oldest_ts is None or min_ts < oldest_ts):
            oldest_ts = min_ts

    if oldest_ts is None:
        return total, 0.0
    if oldest_ts.tzinfo is None:  # pragma: no cover - defensive; columns are tz-aware
        oldest_ts = oldest_ts.replace(tzinfo=timezone.utc)
    age_seconds = (_now() - oldest_ts).total_seconds()
    return total, max(age_seconds, 0.0)


def _classify_os_error(exc: OSError) -> str:
    if isinstance(exc, PermissionError):
        return "permission_denied"
    return "filesystem_error"


def _record_event(
    db: Session,
    *,
    config: _RunTypeConfig,
    organization_id,
    run_id,
    outcome: str,
    dry_run: bool,
    retention_window_days: int,
    failure_reason_code: str | None = None,
    artifact_size_bytes: int | None = None,
) -> None:
    """Insert one ArtifactRetentionEvent, already at its terminal
    outcome -- this module never inserts a row at outcome='started' and
    updates it later in a second statement; the claim, the deletion
    attempt, and the terminal outcome are all decided before this is
    called, inside the same transaction. See the module docstring's
    PURGE_PENDING note for why no separate 'started' row is needed."""
    now = _now()
    db.add(
        ArtifactRetentionEvent(
            organization_id=organization_id,
            outcome=outcome,
            failure_reason_code=failure_reason_code,
            dry_run=dry_run,
            retention_window_days_applied=retention_window_days,
            artifact_size_bytes=artifact_size_bytes,
            completed_at=now,
            **{config.event_run_id_field: run_id},
        )
    )


def _process_one_run_type(
    db: Session,
    config: _RunTypeConfig,
    *,
    budget: int,
    dry_run: bool,
    cutoff: datetime,
    window_days: int,
) -> tuple[int, int, int, int, int, int]:
    """Bounded loop over one run table, mirroring
    scheduler.run_due_schedules()'s per-artifact transaction shape
    exactly. `budget` is this call's share of purge_expired_artifacts()'s
    single, whole-pass RETENTION_CLAIM_BATCH_SIZE allowance -- the
    caller passes in whatever is left of that shared budget, not a fixed
    per-run-type allowance, so the loop below stops at exactly `budget`
    attempts regardless of which run type it's processing. Returns
    (purged, already_missing, failed, dry_run_would_purge,
    bytes_reclaimed, attempted) for this run type only -- `attempted` is
    returned so the caller can deduct it from the shared budget before
    moving on to the next run type."""
    model = config.model
    settings = get_settings()
    storage = get_artifact_storage()
    decision_ts = _decision_timestamp_expression(model)

    purged = already_missing = failed = would_purge = 0
    bytes_reclaimed = 0
    attempted = 0
    excluded_ids: set = set()

    while attempted < budget:
        query = (
            select(model.id, model.organization_id, model.output_file_path)
            .where(
                model.status.in_(_TERMINAL_STATUSES),
                model.output_deleted_at.is_(None),
                decision_ts.is_not(None),
                decision_ts <= cutoff,
            )
        )
        if excluded_ids:
            query = query.where(model.id.not_in(list(excluded_ids)))
        query = query.order_by(decision_ts.asc(), model.id.asc()).limit(1)
        if _supports_skip_locked(db):
            query = query.with_for_update(skip_locked=True, of=model)
        else:  # pragma: no cover - sandbox-only fallback, see engine.py's own docstring
            query = query.with_for_update()

        row = db.execute(query).first()
        if row is None:
            db.commit()  # release lock state; no-op if nothing was locked
            break

        attempted += 1
        run_id, organization_id, output_file_path = row

        try:
            outcome, reason_code, size_bytes, set_deleted_at = _evaluate_and_act(
                db=db,
                config=config,
                run_id=run_id,
                organization_id=organization_id,
                output_file_path=output_file_path,
                dry_run=dry_run,
                storage=storage,
                settings=settings,
            )

            if set_deleted_at:
                result = db.execute(
                    update(model)
                    .where(model.id == run_id, model.output_deleted_at.is_(None))
                    .values(output_deleted_at=_now())
                )
                if result.rowcount != 1:
                    # Lost the race (should be impossible under SKIP
                    # LOCKED -- the row lock is held for this whole
                    # transaction -- but the guard makes it safe
                    # regardless, mirroring scheduler.py's identical
                    # next_run_at guard). Roll back rather than persist
                    # a second terminal event for an artifact another
                    # transaction already finalized; try a different
                    # candidate for the rest of this pass.
                    db.rollback()
                    logger.warning(
                        "Retention lost the output_deleted_at race for %s run %s "
                        "(org %s); excluding it for the remainder of this pass",
                        config.artifact_type, run_id, organization_id,
                    )
                    excluded_ids.add(run_id)
                    continue

            _record_event(
                db,
                config=config,
                organization_id=organization_id,
                run_id=run_id,
                outcome=outcome,
                dry_run=dry_run,
                retention_window_days=window_days,
                failure_reason_code=reason_code,
                artifact_size_bytes=size_bytes,
            )
            db.commit()

            # A failed outcome (and any dry-run outcome, which never sets
            # output_deleted_at) leaves this candidate still matching the
            # eligibility query -- exclude it for the rest of THIS pass
            # so the batch budget is spent across distinct candidates
            # rather than re-selecting the same unresolved row up to
            # batch_size times. It remains eligible again on the NEXT
            # pass (real or dry-run), which is the intended retry
            # behavior for 'failed', and the intended re-audit behavior
            # for dry runs (see the module docstring).
            if dry_run or outcome == "failed":
                excluded_ids.add(run_id)

            if outcome == "completed" and not dry_run:
                purged += 1
                bytes_reclaimed += size_bytes or 0
            elif outcome == "completed" and dry_run:
                would_purge += 1
            elif outcome == "already_missing":
                already_missing += 1
            elif outcome == "failed":
                failed += 1

        except Exception:  # noqa: BLE001 - one artifact's failure must not abort the pass
            db.rollback()
            logger.exception(
                "Retention pass hit an unexpected error processing %s run %s "
                "(org %s); rolled back and excluded it for the remainder of "
                "this pass",
                config.artifact_type, run_id, organization_id,
            )
            excluded_ids.add(run_id)
            continue

    return purged, already_missing, failed, would_purge, bytes_reclaimed, attempted


def _evaluate_and_act(
    *,
    db: Session,
    config: _RunTypeConfig,
    run_id,
    organization_id,
    output_file_path: str,
    dry_run: bool,
    storage,
    settings,
) -> tuple[str, str | None, int | None, bool]:
    """Resolves the artifact's safe path and either deletes it (real
    pass) or checks it (dry run), never both. Returns
    (outcome, failure_reason_code, artifact_size_bytes, set_deleted_at) --
    set_deleted_at is True only for a real pass's completed/already_missing
    outcome; dry runs and failures never set it (see the module
    docstring's PURGED/dry-run notes).

    Deliberately does not perform storage.exists() then storage.delete()
    for the real path -- storage.delete()'s own return value already
    distinguishes "existed and was removed" from "already missing" in
    one call, avoiding an unnecessary and racy check-then-delete. exists()
    is used only for the dry-run path, where a mutating delete() call
    would be a contradiction in terms."""
    tenant_root = Path(getattr(settings, config.output_root_setting)) / str(organization_id)

    try:
        resolved_path = resolve_artifact_path(tenant_root, output_file_path)
    except ArtifactPathError:
        # Defense-in-depth only -- output_file_path is always written
        # server-side by CleaningHandler/StandardizationHandler/
        # ExportHandler, never client-supplied. Never logs the path
        # itself, matching Module 10's own high-severity-log discipline.
        logger.error(
            "Retention path containment violation: artifact_type=%s run_id=%s org_id=%s",
            config.artifact_type, run_id, organization_id,
        )
        return "failed", "unsafe_path", None, False

    if dry_run:
        try:
            present = storage.exists(resolved_path)
        except OSError as exc:
            reason = _classify_os_error(exc)
            logger.error(
                "Retention dry-run existence check failed: artifact_type=%s run_id=%s "
                "org_id=%s reason=%s exc_type=%s",
                config.artifact_type, run_id, organization_id, reason, type(exc).__name__,
            )
            return "failed", reason, None, False

        if not present:
            return "already_missing", None, None, False

        size_bytes = _best_effort_size(resolved_path)
        return "completed", None, size_bytes, False

    # Real pass: capture size before attempting deletion (best-effort --
    # a failed stat must not by itself block the actual delete attempt).
    size_bytes = _best_effort_size(resolved_path)

    try:
        deleted = storage.delete(resolved_path)
    except OSError as exc:
        reason = _classify_os_error(exc)
        logger.error(
            "Retention deletion failed: artifact_type=%s run_id=%s org_id=%s "
            "reason=%s exc_type=%s",
            config.artifact_type, run_id, organization_id, reason, type(exc).__name__,
        )
        return "failed", reason, None, False

    if deleted:
        return "completed", None, size_bytes, True
    # Already missing: report no size for a file we didn't actually
    # observe being removed, even if a size happened to be captured a
    # moment earlier -- avoids reporting a stale/misleading byte count
    # for an artifact this pass did not itself confirm removing.
    return "already_missing", None, None, True


def _best_effort_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def purge_expired_artifacts(
    db: Session,
    *,
    batch_size: int | None = None,
    dry_run: bool | None = None,
) -> RetentionPassResult:
    """Process up to `batch_size` eligible artifacts TOTAL, across all
    three run types (cleaning, standardization, export) combined -- a
    single, shared, whole-pass budget, exactly mirroring
    worker_claim_batch_size's and scheduler_claim_batch_size's own
    convention (see RETENTION_CLAIM_BATCH_SIZE in app.core.config). Run
    types are processed in a fixed order (cleaning, then standardization,
    then export -- see _RUN_TYPE_CONFIGS), each drawing against whatever
    remains of the shared budget after the previous run type's attempts
    were deducted; once the shared budget reaches zero, no further run
    type is queried at all.

    Returns immediately with an all-zero result, touching no row and
    opening no query, if OUTPUT_RETENTION_ENABLED is false -- this is
    the eligibility requirement's own first condition, enforced here
    (not only by whatever caller decides whether to invoke this
    function at all), so calling this function is always safe
    regardless of caller.

    batch_size defaults to settings.retention_claim_batch_size; dry_run
    defaults to settings.output_retention_dry_run. Both may be
    overridden explicitly, e.g. by a future manual-trigger endpoint
    wanting to force a real pass regardless of the configured default.

    Module 13 Phase 5 (metrics only, no behavior change): every call --
    including the disabled early-return below -- increments
    metrics.retention_passes_total exactly once and observes
    metrics.retention_pass_duration_seconds exactly once, both pure
    in-memory operations that do not touch the "opens no query" guarantee
    the disabled path documents below. Every other retention metric is
    updated in bulk, once, from this function's own final tallies (never
    inside the per-artifact loop in _process_one_run_type) -- see
    app.worker.metrics's Module 13 block for why that makes
    double-counting structurally impossible.
    """
    pass_started_at = time.monotonic()
    metrics.retention_passes_total.inc()
    settings = get_settings()
    if not settings.output_retention_enabled:
        metrics.retention_pass_duration_seconds.observe(time.monotonic() - pass_started_at)
        return RetentionPassResult(
            purged_count=0,
            already_missing_count=0,
            failed_count=0,
            dry_run_would_purge_count=0,
            bytes_reclaimed=0,
            dry_run=dry_run if dry_run is not None else settings.output_retention_dry_run,
        )

    if batch_size is None:
        batch_size = settings.retention_claim_batch_size
    if dry_run is None:
        dry_run = settings.output_retention_dry_run

    window_days = settings.output_retention_window_days
    cutoff = _now() - timedelta(days=window_days)

    purged = already_missing = failed = would_purge = 0
    bytes_reclaimed = 0
    remaining_budget = batch_size

    for config in _RUN_TYPE_CONFIGS:
        if remaining_budget <= 0:
            # Shared budget already exhausted by an earlier run type in
            # this same pass -- stop entirely rather than giving this
            # (or any later) run type its own fresh allowance.
            break
        p, am, f, wp, br, attempted = _process_one_run_type(
            db,
            config,
            budget=remaining_budget,
            dry_run=dry_run,
            cutoff=cutoff,
            window_days=window_days,
        )
        purged += p
        already_missing += am
        failed += f
        would_purge += wp
        bytes_reclaimed += br
        remaining_budget -= attempted

    result = RetentionPassResult(
        purged_count=purged,
        already_missing_count=already_missing,
        failed_count=failed,
        dry_run_would_purge_count=would_purge,
        bytes_reclaimed=bytes_reclaimed,
        dry_run=dry_run,
    )
    if result.candidates_considered:
        logger.info(
            "Retention pass (dry_run=%s) considered %d artifact(s): "
            "purged=%d already_missing=%d failed=%d would_purge=%d bytes_reclaimed=%d",
            dry_run, result.candidates_considered, purged, already_missing, failed,
            would_purge, bytes_reclaimed,
        )

    # Module 13 Phase 5 (metrics only): bulk, once-per-pass updates from
    # this function's own already-computed final tallies -- see this
    # function's own docstring and app.worker.metrics's Module 13 block
    # for why this ordering makes double-counting structurally
    # impossible. Only reached on the enabled path (the disabled
    # early-return above already recorded its own pass/duration metrics
    # and returned before this point).
    metrics.retention_artifacts_eligible_total.inc(result.candidates_considered)
    metrics.retention_artifacts_purged_total.inc(purged)
    metrics.retention_artifacts_already_missing_total.inc(already_missing)
    metrics.retention_purge_failures_total.inc(failed)
    metrics.retention_dry_run_artifacts_total.inc(would_purge)
    metrics.retention_bytes_reclaimed_total.inc(bytes_reclaimed)

    backlog_count, oldest_age_seconds = _compute_backlog_snapshot(db, cutoff)
    metrics.retention_backlog_artifacts.set(backlog_count)
    metrics.retention_oldest_eligible_artifact_age_seconds.set(oldest_age_seconds)

    metrics.retention_pass_duration_seconds.observe(time.monotonic() - pass_started_at)
    return result
