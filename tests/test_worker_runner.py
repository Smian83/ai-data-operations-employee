"""Module 13 Phase 4 tests for the worker-loop retention integration in
app.worker.runner.run_forever: the new retention timer fires on its own
schedule (mirroring the existing scheduler/reaper timers already covered
by production use, though not previously unit-tested directly since
run_forever itself is `# pragma: no cover` -- an intentionally infinite
loop with no natural exit point for a test).

Testing strategy: run_forever() never returns on its own, so every test
here forces a deterministic single-iteration exit by monkeypatching
time.sleep to raise a private sentinel exception once claim_batch (itself
monkeypatched to return no TaskRuns) causes the loop to reach its
`if not claimed: time.sleep(...)` branch. Every other worker-loop
dependency (SessionLocal, claim_batch, run_due_schedules,
reap_expired_runs, configure_logging) is monkeypatched to an inert stand-in
so these tests exercise ONLY the new retention-timer wiring, not the real
scheduler/reaper/task-execution logic (each already covered by its own
test file)."""
from unittest.mock import MagicMock

import pytest

import app.worker.runner as runner_module
from app.core.config import get_settings


class _StopLoop(Exception):
    """Sentinel used to deterministically break out of run_forever's
    otherwise-infinite `while True` loop after exactly one iteration."""


def _patch_common(monkeypatch, *, retention_side_effect=None):
    """Shared monkeypatching for every test in this file: an inert
    SessionLocal, no-op configure_logging/scheduler/reaper, claim_batch
    returning no TaskRuns (so the loop always reaches the sleep-based
    exit), and time.sleep raising _StopLoop on its first call so
    run_forever returns control to the test after one full iteration."""
    fake_db = MagicMock()
    monkeypatch.setattr(runner_module, "SessionLocal", lambda: fake_db)
    monkeypatch.setattr(runner_module, "configure_logging", lambda: None)
    monkeypatch.setattr(runner_module, "run_due_schedules", MagicMock())
    monkeypatch.setattr(runner_module, "reap_expired_runs", MagicMock())
    monkeypatch.setattr(runner_module, "claim_batch", MagicMock(return_value=[]))

    purge_mock = MagicMock()
    if retention_side_effect is not None:
        purge_mock.side_effect = retention_side_effect
    monkeypatch.setattr(runner_module, "purge_expired_artifacts", purge_mock)

    def _raise_stop(*args, **kwargs):
        raise _StopLoop()

    monkeypatch.setattr(runner_module.time, "sleep", _raise_stop)

    return fake_db, purge_mock


# --- retention timer fires when due ----------------------------------------


def test_retention_timer_invokes_purge_when_due(monkeypatch) -> None:
    monkeypatch.setenv("RETENTION_POLL_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("RETENTION_CLAIM_BATCH_SIZE", "25")
    monkeypatch.setenv("OUTPUT_RETENTION_DRY_RUN", "false")
    get_settings.cache_clear()
    try:
        fake_db, purge_mock = _patch_common(monkeypatch)

        # last_retention starts at 0.0 inside run_forever; a large
        # monotonic value guarantees "now - last_retention" comfortably
        # exceeds the 60s poll interval on the very first iteration,
        # regardless of the real system clock.
        monkeypatch.setattr(runner_module.time, "monotonic", lambda: 999_999.0)

        with pytest.raises(_StopLoop):
            runner_module.run_forever()

        purge_mock.assert_called_once_with(
            fake_db, batch_size=25, dry_run=False
        )
    finally:
        get_settings.cache_clear()


def test_retention_timer_does_not_fire_before_the_poll_interval_elapses(
    monkeypatch,
) -> None:
    monkeypatch.setenv("RETENTION_POLL_INTERVAL_SECONDS", "3600")
    get_settings.cache_clear()
    try:
        fake_db, purge_mock = _patch_common(monkeypatch)

        # now - last_retention == 0.0 - 0.0 == 0.0, which is less than
        # any legal retention_poll_interval_seconds value (config
        # enforces a minimum of 60.0) -- retention must NOT run on this
        # iteration.
        monkeypatch.setattr(runner_module.time, "monotonic", lambda: 0.0)

        with pytest.raises(_StopLoop):
            runner_module.run_forever()

        purge_mock.assert_not_called()
    finally:
        get_settings.cache_clear()


# --- retention failures never stop the worker -------------------------------


def test_retention_pass_exception_is_isolated_and_worker_continues(
    monkeypatch,
) -> None:
    """A retention-pass failure must be caught, rolled back, and logged --
    never allowed to propagate out of run_forever and crash the worker
    process. Proven here by asserting the loop reaches its normal
    sleep-based exit point (_StopLoop) rather than the RuntimeError
    purge_expired_artifacts raises."""
    monkeypatch.setenv("RETENTION_POLL_INTERVAL_SECONDS", "60")
    get_settings.cache_clear()
    try:
        fake_db, purge_mock = _patch_common(
            monkeypatch, retention_side_effect=RuntimeError("simulated retention failure")
        )
        monkeypatch.setattr(runner_module.time, "monotonic", lambda: 999_999.0)

        # If the exception were not caught inside run_forever, this
        # would raise RuntimeError instead of _StopLoop.
        with pytest.raises(_StopLoop):
            runner_module.run_forever()

        purge_mock.assert_called_once()
        fake_db.rollback.assert_called_once()
        # The loop must still reach db.close() in its finally block for
        # this same iteration, exactly as it would for any other
        # exception-free iteration.
        fake_db.close.assert_called_once()
    finally:
        get_settings.cache_clear()


def test_retention_pass_exception_does_not_prevent_claim_batch_same_iteration(
    monkeypatch,
) -> None:
    """A retention-pass failure in one iteration must not block that same
    iteration's claim_batch call -- the two are independent background
    passes, not a pipeline where one failing should stop the other."""
    monkeypatch.setenv("RETENTION_POLL_INTERVAL_SECONDS", "60")
    get_settings.cache_clear()
    try:
        _fake_db, purge_mock = _patch_common(
            monkeypatch, retention_side_effect=RuntimeError("simulated retention failure")
        )
        monkeypatch.setattr(runner_module.time, "monotonic", lambda: 999_999.0)

        with pytest.raises(_StopLoop):
            runner_module.run_forever()

        purge_mock.assert_called_once()
        runner_module.claim_batch.assert_called_once()
    finally:
        get_settings.cache_clear()


# --- no redundant enabled-check was added in runner.py ----------------------


def test_runner_does_not_duplicate_the_output_retention_enabled_check(
    monkeypatch,
) -> None:
    """purge_expired_artifacts() already no-ops internally when
    OUTPUT_RETENTION_ENABLED is false (returns an all-zero result without
    touching a row). This test proves run_forever calls it unconditionally
    once due -- it must NOT skip the call itself based on
    output_retention_enabled, since that would be a second, redundant
    (and potentially drifting) copy of a check purge_expired_artifacts
    already owns."""
    monkeypatch.setenv("RETENTION_POLL_INTERVAL_SECONDS", "60")
    monkeypatch.setenv("OUTPUT_RETENTION_ENABLED", "false")
    get_settings.cache_clear()
    try:
        _fake_db, purge_mock = _patch_common(monkeypatch)
        monkeypatch.setattr(runner_module.time, "monotonic", lambda: 999_999.0)

        with pytest.raises(_StopLoop):
            runner_module.run_forever()

        # run_forever calls purge_expired_artifacts regardless of
        # output_retention_enabled -- the mock stands in for the real
        # function, so this only proves the CALL happens; the real
        # function's own internal enabled-check is covered by
        # test_retention_worker.py.
        purge_mock.assert_called_once()
    finally:
        get_settings.cache_clear()
