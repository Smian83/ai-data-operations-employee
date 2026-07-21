"""
Module 9: deterministic materialization of an approved Module 8 MatchRun
into a deduplicated output dataset, described in
docs/module-9-data-export-engine-design.md Sections 1, 6, 8, 13.
`materialize()` is pure -- no I/O, no randomness, no AI/ML -- so it always
produces an identical MaterializeResult for identical input and group
data (this engine's determinism acceptance criterion), and is naturally
idempotent. ExportHandler (app.worker.handlers.export) is the only
caller; all persistence and file I/O happen there, not here.

Reserved provenance columns (added per architectural review): every
output row carries __aiops_canonical_record (always True in this
release -- only canonical/ungrouped rows are ever exported) and
__aiops_source_row_index (that row's index in the standardized input),
appended as the final two columns. These names are reserved and
non-configurable; find_reserved_column_collisions() is the deterministic
collision check ExportHandler must run against the input header BEFORE
any group-loading, materialization, or file writing -- a collision is
always a permanent failure, never a silent rename/suffix/overwrite (see
design doc's Required Clarification on provenance column collisions).
"""
from __future__ import annotations

import uuid

from app.export.types import ExportLimits, GroupInput, MaterializeResult, RowExclusion

# Bumped whenever any function's OUTPUT could change for existing input.
EXPORT_ENGINE_VERSION = "1.0"

# Fixed deterministic constant identifying the exported CSV's column
# schema version (design doc's required Change 1). Starts at 1; would
# only increment if a future module changes the export column schema.
EXPORT_CSV_FORMAT_VERSION = 1

# Reserved, non-configurable provenance column names appended to every
# exported row. Namespaced with "__aiops_" specifically so they cannot be
# mistaken for ordinary customer columns.
RESERVED_CANONICAL_RECORD_COLUMN = "__aiops_canonical_record"
RESERVED_SOURCE_ROW_INDEX_COLUMN = "__aiops_source_row_index"
RESERVED_COLUMNS = (RESERVED_CANONICAL_RECORD_COLUMN, RESERVED_SOURCE_ROW_INDEX_COLUMN)


def find_reserved_column_collisions(headers: list[str]) -> list[str]:
    """Returns every reserved provenance column name that already exists
    in the standardized input's header, in header order. Empty list means
    it is safe to proceed. Never renames, suffixes, or otherwise resolves
    a collision -- that is the caller's (ExportHandler's) job, and the
    only resolution it is allowed to apply is to fail permanently."""
    return [h for h in headers if h in RESERVED_COLUMNS]


def materialize(
    rows: list[list[str]],
    headers: list[str],
    groups: list[GroupInput],
    limits: ExportLimits,
) -> MaterializeResult:
    """Deterministically materialize `rows` against `groups`: every
    duplicate group contributes exactly its canonical row; every row not
    named as a non-canonical member of any group passes through
    unchanged; original row order is preserved for everything that
    survives. Two reserved provenance columns are appended to every
    surviving row. Callers MUST have already confirmed
    find_reserved_column_collisions(headers) is empty before calling this
    -- materialize() does not re-check.
    """
    excluded_by_row: dict[int, GroupInput] = {}
    for group in groups:
        for member in group.member_row_indices:
            if member != group.canonical_row_index:
                excluded_by_row[member] = group

    output_headers = list(headers) + list(RESERVED_COLUMNS)
    output_rows: list[list[str]] = []
    exclusions: list[RowExclusion] = []

    for row_index, row in enumerate(rows):
        group = excluded_by_row.get(row_index)
        if group is not None:
            if len(exclusions) < limits.max_persisted_exclusions:
                exclusions.append(
                    RowExclusion(
                        row_index=row_index,
                        match_group_id=group.match_group_id,
                        canonical_row_index=group.canonical_row_index,
                        reason=(
                            f"row {row_index} excluded: member of duplicate group "
                            f"(canonical row_index={group.canonical_row_index}, "
                            f"{group.record_count} members)"
                        ),
                    )
                )
            continue
        # canonical_record is always True in this release -- only
        # canonical/ungrouped rows are ever written here (non-goal:
        # supporting any other value is deferred, see design doc
        # Section 5).
        output_rows.append(list(row) + ["True", str(row_index)])

    row_count = len(output_rows)
    excluded_row_count = len(rows) - row_count

    return MaterializeResult(
        output_headers=output_headers,
        output_rows=output_rows,
        row_count=row_count,
        excluded_row_count=excluded_row_count,
        duplicate_groups_materialized_count=len(groups),
        exclusions=exclusions,
        output_column_count=len(output_headers),
    )
