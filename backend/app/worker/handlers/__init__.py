"""Execution handler registry.

Module 5 replaces only the SYNC no-op with the approved CSV profiling
handler. TRANSFORM, EXPORT and OTHER remain unchanged diagnostics.
"""
from app.models.enums import TaskType
from app.worker.handlers.base import ExecutionHandler
from app.worker.handlers.csv_profiling import CsvProfilingHandler
from app.worker.handlers.noop import NoOpHandler

HANDLER_REGISTRY: dict[TaskType, ExecutionHandler] = {
    TaskType.SYNC: CsvProfilingHandler(),
    TaskType.TRANSFORM: NoOpHandler(),
    TaskType.EXPORT: NoOpHandler(),
    TaskType.OTHER: NoOpHandler(),
}


def get_handler(task_type: TaskType) -> ExecutionHandler:
    try:
        return HANDLER_REGISTRY[task_type]
    except KeyError:
        raise PermanentHandlerLookupError(
            f"No execution handler registered for task_type={task_type}"
        )


class PermanentHandlerLookupError(Exception):
    """Raised when a Task's task_type has no registered handler. Treated by
    the runner as a permanent failure -- retrying will never help."""
