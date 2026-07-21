"""Internal immutable value objects for the Module 9 export engine. Pure
dataclasses, no I/O -- mirrors app.matching.types/app.standardization.types'
shape."""
import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class ExportLimits:
    max_persisted_exclusions: int


@dataclass(frozen=True)
class GroupInput:
    """One Module 8 duplicate group being materialized, as reconstructed
    by ExportHandler from MatchGroup + MatchDecision rows (the same
    reconstruction method Module 8's own audit endpoints already use --
    see design doc Section 6/8). member_row_indices includes the
    canonical row itself; the engine derives the excluded (non-canonical)
    members by subtracting canonical_row_index."""

    match_group_id: uuid.UUID
    canonical_row_index: int
    record_count: int
    member_row_indices: tuple[int, ...]


@dataclass(frozen=True)
class RowExclusion:
    """One row excluded from the materialized output -- mirrors
    ExportRowExclusion's columns exactly (design doc Section 7)."""

    row_index: int
    match_group_id: uuid.UUID
    canonical_row_index: int
    reason: str


@dataclass(frozen=True)
class MaterializeResult:
    output_headers: list[str]
    output_rows: list[list[str]]
    row_count: int
    excluded_row_count: int
    duplicate_groups_materialized_count: int
    # Bounded to ExportLimits.max_persisted_exclusions -- excluded_row_
    # count above is always the true total even when this list is capped.
    exclusions: list[RowExclusion]
    output_column_count: int
