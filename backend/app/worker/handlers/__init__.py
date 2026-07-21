"""Handler registry: maps TaskType -> ExecutionHandler. Adding a real
connector for a task type means writing a new handler module and
registering it here -- the engine itself never changes.

Module 5 update: SYNC now maps to CsvProfilingHandler, the first real
(non-diagnostic) handler -- exactly the "follow-up module" Module 4's own
docstring called out. This is a deliberate behavior change, not a purely
additive one: any Task with task_type=SYNC now executes real CSV-profiling
logic instead of a no-op, and requires an active CSV_UPLOAD data source or
fails permanently (see CsvProfilingHandler.execute).

Module 6 update: TRANSFORM now maps to CleaningHandler, following the
exact same pattern -- also a deliberate, non-additive behavior change: any
Task with task_type=TRANSFORM now executes real CSV-cleaning logic instead
of a no-op, and requires a completed DataProfile for the TaskRun's
source_task_run_id or fails permanently (see CleaningHandler.execute).
EXPORT and OTHER remain on NoOpHandler until their own follow-up
modules."""
from app.models.enums import TaskType
from app.worker.handlers.base import ExecutionHandler
from app.worker.handlers.cleaning import CleaningHandler
from app.worker.handlers.csv_profiling import CsvProfilingHandler
from app.worker.handlers.noop import NoOpHandler

HANDLER_REGISTRY: dict[TaskType, ExecutionHandler] = {
    TaskType.SYNC: CsvProfilingHandler(),
    TaskType.TRANSFORM: CleaningHandler(),
    TaskType.EXPORT: NoOpHandler(),
    TaskType.OTHER: NoOpHandler(),
}


def get_handler(task_type: TaskType) -> ExecutionHandler:
    try:
        return HANDLER_REGISTRY[task_type]
    except KeyError:
        raise PermanentHandlerLookupError(f"No execution handler registered for task_type={task_type}")


class PermanentHandlerLookupError(Exception):
    """Raised when a Task's task_type has no registered handler. Treated by
    the runner as a permanent failure -- retrying will never help."""
