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
