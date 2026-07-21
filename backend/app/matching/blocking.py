"""Stage 2 blocking (Section 6): bucket candidate rows by a single,
highest-weight configured field's normalized value, so only rows sharing
an identical, non-empty bucket are ever compared. Bounds comparison work
to the sum of each block's size^2 rather than the full dataset's n^2."""
from app.cleaning.rules import trim_whitespace
from app.matching.types import MatchRuleFieldSpec


def normalize_blocking_value(value: str) -> str:
    """Trim + collapse internal whitespace (reusing
    app.cleaning.rules.trim_whitespace, the same defensive second-pass
    Module 7 already applies), then casefold for case-insensitive
    bucketing."""
    collapsed, _ = trim_whitespace(value)
    return collapsed.casefold()


def select_blocking_field(fields: tuple[MatchRuleFieldSpec, ...]) -> MatchRuleFieldSpec:
    """The single highest-weight field is the blocking key. Ties are
    broken by earliest position in `fields` -- the handler is responsible
    for loading fields in true creation order (created_at, then id) so
    this positional tie-break is equivalent to "lowest id, i.e. creation
    order" (Section 6), without relying on uuid4 id values being
    comparable as a creation-order proxy."""
    best = fields[0]
    for candidate in fields[1:]:
        if candidate.weight > best.weight:
            best = candidate
    return best


def _column_index(headers: list[str], column_name: str) -> int | None:
    target = column_name.strip().casefold()
    for i, header in enumerate(headers):
        if header.strip().casefold() == target:
            return i
    return None


def build_blocks(
    candidate_row_indices: list[int],
    rows: list[list[str]],
    headers: list[str],
    blocking_field: MatchRuleFieldSpec,
) -> dict[str, list[int]]:
    """Bucket candidate rows by their normalized blocking-key value. Rows
    with a blank blocking-key value are never matched against anything by
    Stage 2 (matching on emptiness would be a guaranteed false-positive
    multiplier) -- they are simply excluded from every bucket."""
    col_index = _column_index(headers, blocking_field.column_name)
    blocks: dict[str, list[int]] = {}
    for row_index in candidate_row_indices:
        raw_value = rows[row_index][col_index] if col_index is not None else ""
        normalized = normalize_blocking_value(raw_value)
        if normalized == "":
            continue
        blocks.setdefault(normalized, []).append(row_index)
    return blocks
