"""
NoOpHandler: a diagnostic handler that does no real work. Its purpose is to
let Module 4's claim/lease/retry/timeout/audit machinery be exercised and
verified end-to-end without also standing up real connector logic (SQL
execution, REST calls, file parsing) against live DataSources -- that is
scoped as a follow-up module per the approved architecture.

It demonstrates the idempotency contract concretely: it "performs" its
one side effect (appending to an in-memory ledger keyed by
idempotency_key) only once per idempotency_key, so re-running the same
logical TaskRun after a simulated crash never double-applies the effect.
This ledger is process-local and exists purely to make the contract
testable -- a real handler's downstream system (a database, an API) would
enforce this itself, keyed on the same idempotency_key.
"""
import logging

from app.worker.handlers.base import ExecutionContext, PermanentExecutionError, TransientExecutionError

logger = logging.getLogger(__name__)

# Process-local, for demonstrating/testing the idempotency contract only.
_applied_idempotency_keys: set[str] = set()


class NoOpHandler:
    def execute(self, context: ExecutionContext) -> str | None:
        metadata = context.task.description or ""
        if "force_transient_failure" in metadata:
            raise TransientExecutionError("Forced transient failure for testing")
        if "force_permanent_failure" in metadata:
            raise PermanentExecutionError("Forced permanent failure for testing")

        if context.idempotency_key in _applied_idempotency_keys:
            logger.info(
                "NoOpHandler: idempotency_key %s already applied -- skipping duplicate effect",
                context.idempotency_key,
            )
            return f"no-op: duplicate execution of {context.idempotency_key} skipped"

        _applied_idempotency_keys.add(context.idempotency_key)
        return f"no-op: executed task {context.task.id} (idempotency_key={context.idempotency_key})"
