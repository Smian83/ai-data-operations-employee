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
