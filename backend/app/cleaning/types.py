"""Internal immutable value objects shared by the cleaning rule engine.
Mirrors app.profiling.types' shape exactly -- pure dataclasses, no I/O."""
from dataclasses import dataclass


@dataclass(frozen=True)
class CleaningLimits:
    max_persisted_changes: int


@dataclass(frozen=True)
class Change:
    """One recorded, deterministic cell-level modification."""

    row_index: int
    column_name: str
    original_value: str
    cleaned_value: str
    rule_name: str
    reason: str
    confidence: float


@dataclass(frozen=True)
class CleaningResult:
    cleaned_rows: list[list[str]]
    # Bounded to CleaningLimits.max_persisted_changes -- total_changes_count
    # below is always the true total even when this list is capped.
    changes: list[Change]
    total_changes_count: int
    changes_by_rule: dict[str, int]
    duplicate_row_count: int
    # Minimum confidence across all applied changes (1.0 if there were
    # none) -- a conservative aggregate, not an average: one uncertain
    # change pulls the reported run confidence down rather than being
    # diluted by many trivial ones.
    confidence_score: float
