"""Module 9 unit tests for the pure export engine
(app.export.engine.materialize / find_reserved_column_collisions). No DB,
no client -- mirrors test_matching_engine.py's discipline."""
import uuid

from app.export.engine import (
    EXPORT_CSV_FORMAT_VERSION,
    RESERVED_CANONICAL_RECORD_COLUMN,
    RESERVED_SOURCE_ROW_INDEX_COLUMN,
    find_reserved_column_collisions,
    materialize,
)
from app.export.types import ExportLimits, GroupInput

HEADERS = ["id", "name", "email"]


def _rows(n: int) -> list[list[str]]:
    return [[str(i), f"name{i}", f"name{i}@example.com"] for i in range(n)]


def _limits(max_persisted_exclusions: int = 10_000) -> ExportLimits:
    return ExportLimits(max_persisted_exclusions=max_persisted_exclusions)


def test_no_duplicate_groups_output_equals_input_plus_provenance_columns():
    rows = _rows(3)
    result = materialize(rows, HEADERS, groups=[], limits=_limits())

    assert result.output_headers == HEADERS + [
        RESERVED_CANONICAL_RECORD_COLUMN,
        RESERVED_SOURCE_ROW_INDEX_COLUMN,
    ]
    assert result.row_count == 3
    assert result.excluded_row_count == 0
    assert result.duplicate_groups_materialized_count == 0
    assert result.exclusions == []
    assert result.output_column_count == len(HEADERS) + 2
    for row_index, output_row in enumerate(result.output_rows):
        assert output_row[-2] == "True"
        assert output_row[-1] == str(row_index)
        assert output_row[:-2] == rows[row_index]


def test_single_group_of_two_excludes_non_canonical_member():
    rows = _rows(4)
    group_id = uuid.uuid4()
    groups = [
        GroupInput(
            match_group_id=group_id,
            canonical_row_index=0,
            record_count=2,
            member_row_indices=(0, 1),
        )
    ]
    result = materialize(rows, HEADERS, groups=groups, limits=_limits())

    assert result.row_count == 3
    assert result.excluded_row_count == 1
    assert result.duplicate_groups_materialized_count == 1
    output_source_indices = [row[-1] for row in result.output_rows]
    assert output_source_indices == ["0", "2", "3"]
    assert len(result.exclusions) == 1
    exclusion = result.exclusions[0]
    assert exclusion.row_index == 1
    assert exclusion.match_group_id == group_id
    assert exclusion.canonical_row_index == 0
    assert "canonical row_index=0" in exclusion.reason
    assert "2 members" in exclusion.reason


def test_chain_style_group_of_five_excludes_all_non_canonical_members():
    rows = _rows(6)
    group_id = uuid.uuid4()
    groups = [
        GroupInput(
            match_group_id=group_id,
            canonical_row_index=1,
            record_count=5,
            member_row_indices=(1, 2, 3, 4, 5),
        )
    ]
    result = materialize(rows, HEADERS, groups=groups, limits=_limits())

    assert result.row_count == 2  # row 0 (ungrouped) + row 1 (canonical)
    assert result.excluded_row_count == 4
    excluded_indices = sorted(e.row_index for e in result.exclusions)
    assert excluded_indices == [2, 3, 4, 5]
    for exclusion in result.exclusions:
        assert exclusion.canonical_row_index == 1


def test_multiple_independent_groups():
    rows = _rows(6)
    group_a = uuid.uuid4()
    group_b = uuid.uuid4()
    groups = [
        GroupInput(group_a, canonical_row_index=0, record_count=2, member_row_indices=(0, 1)),
        GroupInput(group_b, canonical_row_index=3, record_count=2, member_row_indices=(3, 4)),
    ]
    result = materialize(rows, HEADERS, groups=groups, limits=_limits())

    assert result.row_count == 4  # 0, 2, 3, 5
    assert result.excluded_row_count == 2
    assert result.duplicate_groups_materialized_count == 2
    output_source_indices = [row[-1] for row in result.output_rows]
    assert output_source_indices == ["0", "2", "3", "5"]


def test_canonical_row_not_first_member_of_group():
    """Canonical row is not row 0 of the file and not row 0 of the
    group's member set -- rules out an off-by-position bug."""
    rows = _rows(5)
    group_id = uuid.uuid4()
    groups = [
        GroupInput(
            match_group_id=group_id,
            canonical_row_index=3,
            record_count=3,
            member_row_indices=(1, 2, 3),
        )
    ]
    result = materialize(rows, HEADERS, groups=groups, limits=_limits())

    output_source_indices = [row[-1] for row in result.output_rows]
    assert output_source_indices == ["0", "3", "4"]
    excluded_indices = sorted(e.row_index for e in result.exclusions)
    assert excluded_indices == [1, 2]
    for exclusion in result.exclusions:
        assert exclusion.canonical_row_index == 3


def test_canonical_record_is_always_true_for_every_surviving_row():
    rows = _rows(5)
    group_id = uuid.uuid4()
    groups = [
        GroupInput(group_id, canonical_row_index=2, record_count=2, member_row_indices=(2, 3))
    ]
    result = materialize(rows, HEADERS, groups=groups, limits=_limits())
    assert all(row[-2] == "True" for row in result.output_rows)
    # No FALSE value ever appears in this release.
    assert all(row[-2] != "False" for row in result.output_rows)


def test_exclusion_cap_bounds_persisted_rows_but_not_the_aggregate_count():
    rows = _rows(10)
    group_id = uuid.uuid4()
    groups = [
        GroupInput(
            group_id,
            canonical_row_index=0,
            record_count=10,
            member_row_indices=tuple(range(10)),
        )
    ]
    result = materialize(rows, HEADERS, groups=groups, limits=_limits(max_persisted_exclusions=3))

    assert result.excluded_row_count == 9  # true, uncapped total
    assert len(result.exclusions) == 3  # capped persisted detail


def test_engine_determinism_same_input_same_groups_yields_identical_result():
    rows = _rows(6)
    group_id = uuid.uuid4()
    groups = [
        GroupInput(group_id, canonical_row_index=1, record_count=3, member_row_indices=(1, 2, 4))
    ]
    result_a = materialize(rows, HEADERS, groups=groups, limits=_limits())
    result_b = materialize(rows, HEADERS, groups=groups, limits=_limits())

    assert result_a.output_rows == result_b.output_rows
    assert result_a.output_headers == result_b.output_headers
    assert [e.row_index for e in result_a.exclusions] == [e.row_index for e in result_b.exclusions]
    assert result_a.row_count == result_b.row_count
    assert result_a.excluded_row_count == result_b.excluded_row_count


# --- Reserved-column collision detection ------------------------------------


def test_no_collision_when_neither_reserved_name_present():
    assert find_reserved_column_collisions(HEADERS) == []


def test_collision_detected_for_canonical_record_name():
    headers = ["id", "name", RESERVED_CANONICAL_RECORD_COLUMN]
    assert find_reserved_column_collisions(headers) == [RESERVED_CANONICAL_RECORD_COLUMN]


def test_collision_detected_for_source_row_index_name():
    headers = ["id", RESERVED_SOURCE_ROW_INDEX_COLUMN, "name"]
    assert find_reserved_column_collisions(headers) == [RESERVED_SOURCE_ROW_INDEX_COLUMN]


def test_collision_detected_for_both_reserved_names_simultaneously():
    headers = [RESERVED_CANONICAL_RECORD_COLUMN, "name", RESERVED_SOURCE_ROW_INDEX_COLUMN]
    collisions = find_reserved_column_collisions(headers)
    assert set(collisions) == {RESERVED_CANONICAL_RECORD_COLUMN, RESERVED_SOURCE_ROW_INDEX_COLUMN}


def test_export_csv_format_version_is_fixed_constant():
    assert EXPORT_CSV_FORMAT_VERSION == 1
