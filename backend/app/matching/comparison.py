"""Stage 2 per-pair field comparison and weighted scoring (Section 6/7).
Two comparison_type values only -- 'exact' (raw byte-for-byte, already-
standardized values) and 'normalized_exact' (trim+casefold first). No
fuzzy, phonetic, edit-distance, or statistical similarity anywhere here."""
from app.matching.blocking import normalize_blocking_value
from app.matching.types import MatchRuleFieldSpec


def _column_index(headers: list[str], column_name: str) -> int | None:
    target = column_name.strip().casefold()
    for i, header in enumerate(headers):
        if header.strip().casefold() == target:
            return i
    return None


def compare_field(
    value_a: str, value_b: str, comparison_type: str
) -> tuple[bool, str, str]:
    """Returns (matched, normalized_value_a, normalized_value_b). For
    comparison_type='exact', the 'normalized' values are the raw values
    unchanged -- there is nothing to normalize; the field_comparisons
    audit column still records what was actually compared either way."""
    if comparison_type == "normalized_exact":
        norm_a = normalize_blocking_value(value_a)
        norm_b = normalize_blocking_value(value_b)
    else:
        norm_a, norm_b = value_a, value_b
    return norm_a == norm_b, norm_a, norm_b


def compare_pair(
    row_a: list[str],
    row_b: list[str],
    headers: list[str],
    fields: tuple[MatchRuleFieldSpec, ...],
) -> tuple[dict, float]:
    """Weighted comparison across every configured field. total_score is
    always in [0, 1]: (sum of matched fields' weights) / (sum of every
    configured field's weight). A rule set with one field is exactly
    "normalized/exact matching"; several is "multi-column composite
    matching" -- both are this same code path, differing only in
    configuration (Section 6)."""
    field_comparisons: dict = {}
    total_weight = 0.0
    matched_weight = 0.0
    for spec in fields:
        col_index = _column_index(headers, spec.column_name)
        value_a = row_a[col_index] if col_index is not None else ""
        value_b = row_b[col_index] if col_index is not None else ""
        matched, norm_a, norm_b = compare_field(value_a, value_b, spec.comparison_type)
        contribution = spec.weight if matched else 0.0
        total_weight += spec.weight
        matched_weight += contribution
        field_comparisons[spec.column_name] = {
            "value_a": norm_a,
            "value_b": norm_b,
            "matched": matched,
            "weight": spec.weight,
            "contribution": contribution,
        }
    total_score = matched_weight / total_weight if total_weight > 0 else 0.0
    return field_comparisons, total_score
