"""
ExecutionHandler: the interface every task-type-specific execution handler
implements. The engine (app.worker.engine, app.worker.runner) never knows
anything about *how* a task type actually does its work -- only that it can
call `handler.execute(context)` and get either a plain return (success) or
one of the two exception types below.

Retryable vs. permanent is a decision the handler makes by choosing which
exception to raise -- the engine does not try to classify errors itself.
"""
from dataclasses import dataclass
from typing import Protocol

from app.models.data_source import DataSource
from app.models.task import Task
from app.models.task_run import TaskRun
from app.worker.credentials import CredentialProvider


class TransientExecutionError(Exception):
    """A failure that is expected to succeed on retry (network blip,
    timeout, rate limit, temporary unavailability). The engine will requeue
    the run for retry if attempts remain."""


class PermanentExecutionError(Exception):
    """A failure that will not be fixed by retrying (bad configuration,
    invalid credentials, malformed data, unsupported operation). The engine
    terminates the run as 'failed' immediately, regardless of remaining
    attempts."""


@dataclass(frozen=True)
class ExecutionContext:
    """Everything a handler needs to execute one TaskRun. `idempotency_key`
    is the value handlers MUST pass to any downstream system whose write
    they perform, so a duplicate execution of the same logical run (e.g. a
    retry after a crash) cannot create a duplicate downstream effect --
    see TaskRun.idempotency_key's docstring for the full rationale."""

    task_run: TaskRun
    task: Task
    data_source: DataSource | None
    idempotency_key: str
    credential_provider: CredentialProvider


class ExecutionHandler(Protocol):
    def execute(self, context: ExecutionContext) -> str | None:
        """Run the task. Return an optional human-readable log/output
        string on success. Raise TransientExecutionError or
        PermanentExecutionError on failure."""
        ...
