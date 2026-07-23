"""Module 12: configuration tests for scheduled task execution --
defaults, bounds, and startup-time (not silently-clamped) rejection of
misconfiguration."""
import pytest
from pydantic import ValidationError

from app.core.config import SCHEDULE_INTERVAL_HARD_FLOOR_SECONDS, Settings, get_settings


def test_scheduler_poll_interval_default() -> None:
    assert get_settings().scheduler_poll_interval_seconds == 15.0


def test_scheduler_claim_batch_size_default() -> None:
    assert get_settings().scheduler_claim_batch_size == 50


def test_minimum_schedule_interval_default() -> None:
    assert get_settings().minimum_schedule_interval_seconds == 60


def test_maximum_schedule_interval_default() -> None:
    assert get_settings().maximum_schedule_interval_seconds == 2_592_000


def test_hard_floor_constant_is_30_seconds() -> None:
    # This constant is hand-kept-in-sync with the database migration's own
    # literal ">= 30" -- see app/core/config.py's own docstring and
    # database/alembic/versions/d5e6f7a8b9c0_scheduled_task_execution.py.
    assert SCHEDULE_INTERVAL_HARD_FLOOR_SECONDS == 30


def test_scheduler_poll_interval_below_minimum_fails_startup() -> None:
    with pytest.raises(ValidationError):
        Settings(SCHEDULER_POLL_INTERVAL_SECONDS=0.5)


def test_scheduler_poll_interval_above_maximum_fails_startup() -> None:
    with pytest.raises(ValidationError):
        Settings(SCHEDULER_POLL_INTERVAL_SECONDS=301.0)


def test_scheduler_claim_batch_size_zero_fails_startup() -> None:
    with pytest.raises(ValidationError):
        Settings(SCHEDULER_CLAIM_BATCH_SIZE=0)


def test_minimum_schedule_interval_below_hard_floor_fails_startup() -> None:
    with pytest.raises(ValidationError):
        Settings(MINIMUM_SCHEDULE_INTERVAL_SECONDS=SCHEDULE_INTERVAL_HARD_FLOOR_SECONDS - 1)


def test_minimum_schedule_interval_at_hard_floor_is_accepted() -> None:
    s = Settings(MINIMUM_SCHEDULE_INTERVAL_SECONDS=SCHEDULE_INTERVAL_HARD_FLOOR_SECONDS)
    assert s.minimum_schedule_interval_seconds == SCHEDULE_INTERVAL_HARD_FLOOR_SECONDS


def test_maximum_lower_than_minimum_rejected() -> None:
    with pytest.raises(ValidationError, match="MAXIMUM_SCHEDULE_INTERVAL_SECONDS"):
        Settings(MINIMUM_SCHEDULE_INTERVAL_SECONDS=100, MAXIMUM_SCHEDULE_INTERVAL_SECONDS=50)


def test_maximum_equal_to_minimum_is_accepted() -> None:
    s = Settings(MINIMUM_SCHEDULE_INTERVAL_SECONDS=120, MAXIMUM_SCHEDULE_INTERVAL_SECONDS=120)
    assert s.maximum_schedule_interval_seconds == 120


def test_invalid_configuration_is_not_silently_clamped() -> None:
    """A below-bound value must raise, never be quietly coerced to the
    nearest valid bound."""
    with pytest.raises(ValidationError):
        Settings(SCHEDULER_CLAIM_BATCH_SIZE=-5)
