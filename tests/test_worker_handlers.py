"""Tests for the handler registry and the NoOpHandler diagnostic handler,
including the idempotency contract: executing the same idempotency_key
twice must not repeat the handler's side effect."""
import uuid

import pytest

from app.models.enums import TaskType
from app.worker.handlers import HANDLER_REGISTRY, PermanentHandlerLookupError, get_handler
from app.worker.handlers.base import ExecutionContext, PermanentExecutionError, TransientExecutionError
from app.worker.handlers.noop import NoOpHandler, _applied_idempotency_keys


class _FakeTask:
    def __init__(self, description: str | None = None) -> None:
        self.id = uuid.uuid4()
        self.description = description


def _context(idempotency_key: str, description: str | None = None) -> ExecutionContext:
    return ExecutionContext(
        task_run=None,
        task=_FakeTask(description=description),
        data_source=None,
        idempotency_key=idempotency_key,
        credential_provider=None,
    )


def test_registry_has_a_handler_for_every_task_type() -> None:
    for task_type in TaskType:
        assert get_handler(task_type) is not None


def test_get_handler_unknown_type_raises() -> None:
    with pytest.raises(PermanentHandlerLookupError):
        get_handler("not-a-real-task-type")


def test_noop_handler_executes_successfully() -> None:
    handler = NoOpHandler()
    key = str(uuid.uuid4())
    result = handler.execute(_context(key))
    assert "executed" in result
    _applied_idempotency_keys.discard(key)  # test isolation


def test_noop_handler_skips_duplicate_idempotency_key() -> None:
    handler = NoOpHandler()
    key = str(uuid.uuid4())
    first = handler.execute(_context(key))
    second = handler.execute(_context(key))
    assert "executed" in first
    assert "duplicate" in second
    _applied_idempotency_keys.discard(key)  # test isolation


def test_noop_handler_raises_transient_on_flag() -> None:
    handler = NoOpHandler()
    with pytest.raises(TransientExecutionError):
        handler.execute(_context(str(uuid.uuid4()), description="force_transient_failure"))


def test_noop_handler_raises_permanent_on_flag() -> None:
    handler = NoOpHandler()
    with pytest.raises(PermanentExecutionError):
        handler.execute(_context(str(uuid.uuid4()), description="force_permanent_failure"))
