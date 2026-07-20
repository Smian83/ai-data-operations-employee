"""Handler registry: maps TaskType -> ExecutionHandler. Adding a real
connector for a task type means writing a new handler module and
registering it here -- the engine itself never changes."""
from app.models.enums import TaskType
from app.worker.handlers.base import ExecutionHandler
from app.worker.handlers.noop import NoOpHandler

# Module 4 ships exactly one handler (see architecture rationale in
# NoOpHandler's docstring). Real SYNC/TRANSFORM/EXPORT handlers against
# live DataSources are a scoped follow-up module.
HANDLER_REGISTRY: dict[TaskType, ExecutionHandler] = {
    TaskType.SYNC: NoOpHandler(),
    TaskType.TRANSFORM: NoOpHandler(),
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
