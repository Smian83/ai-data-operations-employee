"""
Execution engine metrics, in Prometheus format.

A dedicated CollectorRegistry (not the global default) is used so that
importing this module repeatedly in tests never raises Prometheus's
"Duplicated timeseries" error, and so the FastAPI process (which does not
run the worker loop) can still expose a queue_depth gauge computed on
demand without carrying unrelated worker-process metrics.
"""
from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram

registry = CollectorRegistry()

tasks_claimed_total = Counter(
    "task_engine_tasks_claimed_total",
    "Total TaskRuns claimed by a worker.",
    registry=registry,
)
tasks_completed_total = Counter(
    "task_engine_tasks_completed_total",
    "Total TaskRuns that finished in the success state.",
    registry=registry,
)
tasks_failed_total = Counter(
    "task_engine_tasks_failed_total",
    "Total TaskRuns that finished in the failed (terminal) state.",
    registry=registry,
)
tasks_retried_total = Counter(
    "task_engine_tasks_retried_total",
    "Total times a failed attempt was requeued for retry rather than "
    "terminated.",
    registry=registry,
)
task_execution_duration_seconds = Histogram(
    "task_engine_execution_duration_seconds",
    "Wall-clock duration of successful TaskRun executions, in seconds.",
    registry=registry,
    buckets=(0.1, 0.5, 1, 5, 15, 30, 60, 120, 300, 600, 1800),
)
queue_depth = Gauge(
    "task_engine_queue_depth",
    "Number of TaskRuns currently in the pending state.",
    registry=registry,
)

# --- Module 12: scheduled task execution ------------------------------------
# Same dedicated-registry, Counter/Gauge convention as above. NOTE: these
# inherit the same pre-existing process-boundary limitation the four
# counters above already have -- app/api/internal.py's /internal/metrics
# endpoint runs inside the FastAPI process and reads THIS module's
# `registry` object directly, but the worker loop (where these are actually
# incremented) runs as a separate OS process with its own, independent copy
# of `registry` in its own memory. tasks_claimed_total/tasks_completed_
# total/tasks_failed_total/tasks_retried_total above are therefore already
# unreachable via /internal/metrics today (only queue_depth is genuinely
# live there, because that one value is computed fresh from a direct DB
# query at request time, not read from an in-memory counter -- see
# app/api/internal.py's own comment). The scheduler counters below are not
# a new gap; closing this pre-existing limitation (e.g. a metrics endpoint
# inside the worker process, or a push-gateway) is out of scope for Module
# 12. Until then, the structured log lines in app/worker/scheduler.py are
# the reliably observable operator signal for scheduler health.
scheduler_passes_total = Counter(
    "task_scheduler_passes_total",
    "Total scheduler poll passes attempted (run_due_schedules invocations).",
    registry=registry,
)
scheduler_runs_created_total = Counter(
    "task_scheduler_runs_created_total",
    "Total TaskRuns created by the scheduler.",
    registry=registry,
)
scheduler_errors_total = Counter(
    "task_scheduler_errors_total",
    "Total per-task scheduling attempts that failed and were rolled back.",
    registry=registry,
)
scheduler_last_success_timestamp_seconds = Gauge(
    "task_scheduler_last_success_timestamp_seconds",
    "Unix timestamp of the last scheduler pass that completed without an "
    "unhandled, pass-level exception. Updates on empty (zero-due-tasks) "
    "passes too -- that is the signal operators need to distinguish "
    "'scheduler idle' from 'scheduler stopped'.",
    registry=registry,
)

# --- Module 13: output artifact retention -----------------------------------
# Same dedicated-registry, Counter/Gauge/Histogram convention as above, and
# the same pre-existing process-boundary limitation noted for the Module 12
# block: these are incremented inside app.worker.retention.
# purge_expired_artifacts(), which runs in the worker process, not the
# FastAPI process /internal/metrics reads from -- see that comment above
# for the full explanation. Every counter below is incremented exactly
# once per pass, in bulk, from purge_expired_artifacts()'s own final,
# already-committed RetentionPassResult tallies (never incremented inside
# the per-artifact loop in _process_one_run_type) -- this is what makes
# double-counting structurally impossible: a count that was never added to
# the result (e.g. an artifact whose transaction rolled back and was
# retried under a different candidate) was never added to a metric either.
# prometheus_client's Counter/Gauge/Histogram objects are internally
# lock-protected (a threading.Lock per metric) and therefore safe to call
# from any thread without additional synchronization here, matching every
# other metric in this module.
retention_passes_total = Counter(
    "retention_passes_total",
    "Total purge_expired_artifacts() invocations, including passes where "
    "OUTPUT_RETENTION_ENABLED is false (a no-op pass) -- this is a worker "
    "liveness signal (\"is the retention timer still firing on schedule\"), "
    "not a signal that any artifact was actually evaluated; see "
    "retention_artifacts_eligible_total for that.",
    registry=registry,
)
retention_artifacts_eligible_total = Counter(
    "retention_artifacts_eligible_total",
    "Total artifacts claimed and evaluated across all passes (the sum of "
    "purged + already_missing + failed + dry-run would-purge outcomes) -- "
    "i.e. RetentionPassResult.candidates_considered, accumulated pass over "
    "pass. Zero for any pass where retention is disabled or nothing "
    "matched the eligibility window.",
    registry=registry,
)
retention_artifacts_purged_total = Counter(
    "retention_artifacts_purged_total",
    "Total artifacts whose output file was actually deleted from disk "
    "(real passes only, dry_run=false) -- never incremented for a "
    "dry-run pass; see retention_dry_run_artifacts_total for that count.",
    registry=registry,
)
retention_artifacts_already_missing_total = Counter(
    "retention_artifacts_already_missing_total",
    "Total artifacts found already absent from disk when a purge was "
    "attempted -- an expected convergence outcome, not a failure. Counted "
    "for both real and dry-run passes, matching "
    "ArtifactRetentionEvent.outcome='already_missing' semantics.",
    registry=registry,
)
retention_purge_failures_total = Counter(
    "retention_purge_failures_total",
    "Total artifacts that failed to purge (permission_denied, "
    "filesystem_error, unsafe_path, or another classified failure "
    "reason). The artifact remains eligible and is retried on a later "
    "pass -- this counter is cumulative across every attempt, so a "
    "single artifact repeatedly failing across passes increments it "
    "more than once.",
    registry=registry,
)
retention_dry_run_artifacts_total = Counter(
    "retention_dry_run_artifacts_total",
    "Total artifacts a dry-run pass determined WOULD have been purged, "
    "had OUTPUT_RETENTION_DRY_RUN been false. Never incremented by a "
    "real pass.",
    registry=registry,
)
retention_bytes_reclaimed_total = Counter(
    "retention_bytes_reclaimed_total",
    "Total bytes actually freed by real (non-dry-run) artifact "
    "deletions. Never incremented for a dry-run pass, an "
    "already_missing outcome (nothing to measure), or a failed outcome.",
    registry=registry,
)
retention_pass_duration_seconds = Histogram(
    "retention_pass_duration_seconds",
    "Wall-clock duration of one purge_expired_artifacts() call, in "
    "seconds -- including passes where retention is disabled (that "
    "duration is near-zero, and is itself a useful data point: a "
    "disabled pass that is NOT near-zero would indicate the early-return "
    "guard is not behaving as documented).",
    registry=registry,
    buckets=(0.01, 0.05, 0.1, 0.5, 1, 5, 15, 30, 60, 120, 300),
)
retention_oldest_eligible_artifact_age_seconds = Gauge(
    "retention_oldest_eligible_artifact_age_seconds",
    "Age, in seconds, of the single oldest artifact that currently "
    "matches the retention eligibility criteria (terminal status, past "
    "the configured window, not yet purged) but has not yet been "
    "purged -- measured as of the end of the most recent enabled pass. "
    "0 when the backlog is empty. Never updated by a pass where "
    "retention is disabled, so this gauge holds its last real value "
    "(or its initial 0) while disabled, rather than falsely reporting "
    "an empty backlog.",
    registry=registry,
)
retention_backlog_artifacts = Gauge(
    "retention_backlog_artifacts",
    "Count of artifacts that currently match the retention eligibility "
    "criteria but remain unpurged, across all three run types combined, "
    "as of the end of the most recent enabled pass -- a snapshot, not a "
    "cumulative counter, so it can go up or down between passes as new "
    "runs age past the window and existing ones get purged. Never "
    "updated by a pass where retention is disabled (same reasoning as "
    "retention_oldest_eligible_artifact_age_seconds above).",
    registry=registry,
)
