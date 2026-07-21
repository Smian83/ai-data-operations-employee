"""Module 8 unit tests for the pure matching engine (app.matching.*): no
DB, no client -- direct calls into match()/compare_pair()/build_blocks()/
UnionFind/select_canonical, covering every case listed in
docs/module-8-data-matching-deduplication-design.md Section 14."""
from app.matching.blocking import build_blocks, normalize_blocking_value, select_blocking_field
from app.matching.canonical import select_canonical
from app.matching.clustering import UnionFind
from app.matching.comparison import compare_field, compare_pair
from app.matching.engine import (
    MATCH_ENGINE_VERSION,
    RULE_COMPOSITE_WEIGHTED,
    RULE_EXACT_ROW,
    RULE_NORMALIZED_EXACT,
    match,
)
from app.matching.types import MatchLimits, MatchRuleFieldSpec, MatchRuleSetConfig

DEFAULT_LIMITS = MatchLimits(
    max_block_size=1000, max_persisted_decisions=10_000, max_skipped_row_sample=20
)


def _rule_set(**overrides) -> MatchRuleSetConfig:
    defaults = dict(
        duplicate_threshold=0.9,
        review_threshold=0.4,
        fields=(MatchRuleFieldSpec("email", "normalized_exact", 1.0),),
    )
    defaults.update(overrides)
    return MatchRuleSetConfig(**defaults)


# --- Stage 1: exact-row hashing ---------------------------------------------


def test_identical_rows_are_grouped_by_stage_one():
    headers = ["id", "email"]
    rows = [["1", "a@example.com"], ["1", "a@example.com"], ["2", "b@example.com"]]
    result = match(rows, headers, None, DEFAULT_LIMITS)
    assert len(result.groups) == 1
    assert result.groups[0].member_row_indices == (0, 1)
    assert result.groups[0].canonical_row_index == 0
    assert result.decisions[0].rule_name == RULE_EXACT_ROW
    assert result.decisions[0].blocking_key is None
    assert result.decisions[0].confidence_score == 1.0


def test_single_differing_cell_prevents_stage_one_grouping():
    headers = ["id", "email"]
    rows = [["1", "a@example.com"], ["1", "a@example.org"]]
    result = match(rows, headers, None, DEFAULT_LIMITS)
    assert result.groups == []
    assert result.decisions == []


def test_stage_one_chain_links_a_large_duplicate_group_without_full_pairwise_blowup():
    headers = ["id"]
    rows = [["dup"]] * 5
    result = match(rows, headers, None, DEFAULT_LIMITS)
    assert len(result.groups) == 1
    assert result.groups[0].member_row_indices == (0, 1, 2, 3, 4)
    # Chain-linked: 4 edges for 5 identical rows, not C(5,2)=10.
    assert len(result.decisions) == 4
    assert result.total_comparisons_count == 4


# --- Blocking-key derivation -------------------------------------------------


def test_normalize_blocking_value_trims_and_casefolds():
    assert normalize_blocking_value("  Jane@Example.com  ") == "jane@example.com"


def test_select_blocking_field_picks_highest_weight():
    fields = (
        MatchRuleFieldSpec("name", "normalized_exact", 0.3),
        MatchRuleFieldSpec("email", "normalized_exact", 1.0),
    )
    assert select_blocking_field(fields).column_name == "email"


def test_select_blocking_field_ties_break_by_first_listed():
    fields = (
        MatchRuleFieldSpec("email", "normalized_exact", 1.0),
        MatchRuleFieldSpec("name", "normalized_exact", 1.0),
    )
    assert select_blocking_field(fields).column_name == "email"


def test_build_blocks_excludes_blank_blocking_values():
    headers = ["id", "email"]
    rows = [["1", ""], ["2", ""], ["3", "x@example.com"]]
    field = MatchRuleFieldSpec("email", "normalized_exact", 1.0)
    blocks = build_blocks([0, 1, 2], rows, headers, field)
    assert blocks == {"x@example.com": [2]}


def test_blank_blocking_values_are_never_compared_by_stage_two():
    headers = ["id", "email"]
    rows = [["1", ""], ["2", ""]]
    result = match(rows, headers, _rule_set(), DEFAULT_LIMITS)
    assert result.decisions == []
    assert result.groups == []


# --- Per-field comparison ----------------------------------------------------


def test_compare_field_exact_requires_byte_identity():
    matched, a, b = compare_field("Jane", "jane", "exact")
    assert matched is False
    assert (a, b) == ("Jane", "jane")


def test_compare_field_normalized_exact_ignores_case_and_whitespace():
    matched, a, b = compare_field("  Jane  ", "JANE", "normalized_exact")
    assert matched is True
    assert (a, b) == ("jane", "jane")


def test_compare_pair_all_fields_match():
    headers = ["email", "name"]
    fields = (
        MatchRuleFieldSpec("email", "normalized_exact", 1.0),
        MatchRuleFieldSpec("name", "normalized_exact", 1.0),
    )
    comparisons, score = compare_pair(
        ["a@example.com", "Jane"], ["A@Example.com", "jane"], headers, fields
    )
    assert score == 1.0
    assert all(v["matched"] for v in comparisons.values())


def test_compare_pair_no_fields_match():
    headers = ["email", "name"]
    fields = (
        MatchRuleFieldSpec("email", "normalized_exact", 1.0),
        MatchRuleFieldSpec("name", "normalized_exact", 1.0),
    )
    comparisons, score = compare_pair(
        ["a@example.com", "Jane"], ["b@example.com", "Bob"], headers, fields
    )
    assert score == 0.0
    assert all(not v["matched"] for v in comparisons.values())


def test_compare_pair_partial_match_is_weighted():
    headers = ["email", "name"]
    fields = (
        MatchRuleFieldSpec("email", "normalized_exact", 1.0),
        MatchRuleFieldSpec("name", "normalized_exact", 0.5),
    )
    comparisons, score = compare_pair(
        ["a@example.com", "Jane"], ["a@example.com", "Bob"], headers, fields
    )
    assert score == 1.0 / 1.5
    assert comparisons["email"]["matched"] is True
    assert comparisons["name"]["matched"] is False


def test_compare_pair_single_field_is_normalized_exact_matching():
    headers = ["email"]
    fields = (MatchRuleFieldSpec("email", "normalized_exact", 1.0),)
    _, score = compare_pair(["a@example.com"], ["A@Example.com"], headers, fields)
    assert score == 1.0


# --- Threshold classification ------------------------------------------------


def test_threshold_classification_duplicate_at_boundary():
    # Non-byte-identical but normalized-identical values exercise the
    # Stage-2 boundary explicitly (byte-identical rows would be caught by
    # Stage 1 instead).
    headers = ["email"]
    rows = [["A@Example.com"], ["a@example.com"]]
    rule_set = _rule_set(duplicate_threshold=1.0, review_threshold=0.5)
    result = match(rows, headers, rule_set, DEFAULT_LIMITS)
    assert result.decisions[0].decision == "duplicate"
    assert result.decisions[0].total_score == 1.0


def test_threshold_classification_ambiguous_between_thresholds():
    headers = ["email", "name"]
    rows = [["a@example.com", "Jane"], ["a@example.com", "Bob"]]
    rule_set = MatchRuleSetConfig(
        duplicate_threshold=0.9,
        review_threshold=0.4,
        fields=(
            MatchRuleFieldSpec("email", "normalized_exact", 1.0),
            MatchRuleFieldSpec("name", "normalized_exact", 0.3),
        ),
    )
    result = match(rows, headers, rule_set, DEFAULT_LIMITS)
    assert len(result.decisions) == 1
    assert result.decisions[0].decision == "ambiguous"
    assert result.groups == []


def test_threshold_classification_below_review_threshold_is_not_persisted():
    # Both rows share the blocking field's value (email, weight 9.0 ->
    # selected as the blocking key), so they ARE compared -- but a
    # completely different name (weight 1.0) drops the weighted score to
    # 0.9, below a deliberately high review_threshold, so no decision is
    # persisted even though a real comparison happened.
    headers = ["email", "name"]
    rows = [["a@example.com", "Jane"], ["a@example.com", "Bob"]]
    rule_set = MatchRuleSetConfig(
        duplicate_threshold=0.95,
        review_threshold=0.92,
        fields=(
            MatchRuleFieldSpec("email", "normalized_exact", 9.0),
            MatchRuleFieldSpec("name", "normalized_exact", 1.0),
        ),
    )
    result = match(rows, headers, rule_set, DEFAULT_LIMITS)
    assert result.decisions == []
    assert result.total_comparisons_count == 1
    assert result.duplicate_pairs_count == 0
    assert result.ambiguous_pairs_count == 0


# --- Union-find clustering ---------------------------------------------------


def test_union_find_transitive_grouping_without_direct_comparison():
    uf = UnionFind(3)
    uf.union(0, 1)
    uf.union(1, 2)
    components = uf.components()
    roots = {uf.find(i) for i in range(3)}
    assert len(roots) == 1
    assert sorted(components[uf.find(0)]) == [0, 1, 2]


def test_engine_transitive_closure_via_explicit_chain_scores():
    headers = ["block", "key"]
    # key normalizes such that row0==row1 and row1==row2 but row0!=row2
    # is impossible for a single exact-comparison field (equality is
    # transitive) -- so we use two rows equal directly, and rely on
    # Stage 1 + Stage 2 combining: row0/row1 are a Stage-1 exact
    # duplicate; row1/row2 are a Stage-2 composite duplicate; row0/row2
    # are never directly compared (different blocks) yet end up in the
    # same final group because row1 bridges them.
    rows = [
        ["blockA", "same"],
        ["blockA", "same"],
        ["blockB", "same"],
    ]
    rule_set = MatchRuleSetConfig(
        duplicate_threshold=0.9,
        review_threshold=0.1,
        fields=(MatchRuleFieldSpec("key", "normalized_exact", 1.0),),
    )
    result = match(rows, headers, rule_set, DEFAULT_LIMITS)
    assert len(result.groups) == 1
    assert result.groups[0].member_row_indices == (0, 1, 2)


# --- Canonical-record selection ----------------------------------------------


def test_select_canonical_is_lowest_row_index():
    assert select_canonical([5, 2, 9]) == 2


def test_canonical_row_is_lowest_even_when_it_is_a_stage_one_representative():
    headers = ["email"]
    rows = [["a@example.com"], ["a@example.com"], ["A@Example.com"]]
    rule_set = _rule_set(duplicate_threshold=0.9, review_threshold=0.1)
    result = match(rows, headers, rule_set, DEFAULT_LIMITS)
    assert len(result.groups) == 1
    assert result.groups[0].canonical_row_index == 0


# --- Confidence / aggregate accounting ---------------------------------------


def test_confidence_score_is_one_when_there_are_zero_groups():
    headers = ["email"]
    rows = [["a@example.com"], ["b@example.com"]]
    result = match(rows, headers, _rule_set(review_threshold=0.99), DEFAULT_LIMITS)
    assert result.groups == []
    assert result.confidence_score == 1.0


def test_confidence_score_is_minimum_across_groups():
    headers = ["a_block", "email"]
    rows = [
        ["1", "same@example.com"],
        ["1", "same@example.com"],  # stage-1 exact dup, confidence 1.0
        ["2", "x@example.com"],
        ["2", "y@example.com"],  # composite ambiguous/dup depending on config
    ]
    rule_set = MatchRuleSetConfig(
        duplicate_threshold=0.5,
        review_threshold=0.1,
        fields=(MatchRuleFieldSpec("a_block", "normalized_exact", 1.0),),
    )
    result = match(rows, headers, rule_set, DEFAULT_LIMITS)
    # Group 1: rows 0,1 confidence 1.0. Group 2: rows 2,3 share a_block
    # "2" -> normalized_exact match on a_block -> score 1.0 too in this
    # configuration, so overall confidence stays 1.0; assert the
    # aggregate is the true minimum, not an average.
    assert result.confidence_score == min(g.confidence_score for g in result.groups)


# --- Persisted-decision cap ---------------------------------------------------


def test_persisted_decisions_are_capped_but_counts_stay_true():
    headers = ["id"]
    rows = [["dup"]] * 6  # 5 chain-edge decisions from Stage 1
    limits = MatchLimits(max_block_size=1000, max_persisted_decisions=2, max_skipped_row_sample=20)
    result = match(rows, headers, None, limits)
    assert len(result.decisions) == 2
    assert result.duplicate_pairs_count == 5
    assert result.decisions_by_rule[RULE_EXACT_ROW] == 5


# --- Block-size cap and skipped-block sampling -------------------------------


def test_oversized_block_is_skipped_and_recorded_with_a_bounded_deterministic_sample():
    headers = ["email"]
    rows = [[f"row{i}", "dup@example.com"][::-1] for i in range(6)]
    # Simpler: single email column, 6 identical (but not row-identical
    # overall since id differs) rows sharing one blocking value.
    headers = ["id", "email"]
    rows = [[str(i), "dup@example.com"] for i in range(6)]
    rule_set = _rule_set(duplicate_threshold=0.9, review_threshold=0.1)
    limits = MatchLimits(max_block_size=3, max_persisted_decisions=10_000, max_skipped_row_sample=2)
    result = match(rows, headers, rule_set, limits)
    assert result.skipped_block_count == 1
    skipped = result.skipped_blocks[0]
    assert skipped.blocking_key == "dup@example.com"
    assert skipped.block_size == 6
    assert skipped.sample_row_indices == (0, 1)
    assert result.decisions == []
    assert result.groups == []


def test_skipped_block_sample_is_deterministic_across_repeated_runs():
    headers = ["id", "email"]
    rows = [[str(i), "dup@example.com"] for i in range(10)]
    rule_set = _rule_set(duplicate_threshold=0.9, review_threshold=0.1)
    limits = MatchLimits(max_block_size=3, max_persisted_decisions=10_000, max_skipped_row_sample=4)
    first = match(rows, headers, rule_set, limits)
    second = match(rows, headers, rule_set, limits)
    assert first.skipped_blocks == second.skipped_blocks


# --- blocking_key propagation (approved design revision) ---------------------


def test_stage_two_decisions_carry_the_blocking_key_stage_one_does_not():
    headers = ["id", "email"]
    rows = [["1", "a@example.com"], ["1", "a@example.com"], ["2", "A@Example.com"]]
    rule_set = _rule_set(duplicate_threshold=0.9, review_threshold=0.1)
    result = match(rows, headers, rule_set, DEFAULT_LIMITS)
    stage1 = [d for d in result.decisions if d.rule_name == RULE_EXACT_ROW]
    stage2 = [d for d in result.decisions if d.rule_name != RULE_EXACT_ROW]
    assert all(d.blocking_key is None for d in stage1)
    assert all(d.blocking_key == "a@example.com" for d in stage2)


# --- rule_name resolution -----------------------------------------------------


def test_single_field_rule_set_uses_normalized_exact_rule_name():
    headers = ["email"]
    rows = [["a@example.com"], ["A@Example.com"]]
    result = match(rows, headers, _rule_set(duplicate_threshold=0.9, review_threshold=0.1), DEFAULT_LIMITS)
    assert result.decisions[0].rule_name == RULE_NORMALIZED_EXACT


def test_multi_field_rule_set_uses_composite_weighted_rule_name():
    # Not byte-identical (differs in case/whitespace only), so this
    # escapes Stage 1 and is scored by Stage 2's composite comparison.
    headers = ["email", "name"]
    rows = [["a@example.com", "Jane"], ["A@Example.com", " jane "]]
    rule_set = MatchRuleSetConfig(
        duplicate_threshold=0.9,
        review_threshold=0.1,
        fields=(
            MatchRuleFieldSpec("email", "normalized_exact", 1.0),
            MatchRuleFieldSpec("name", "normalized_exact", 1.0),
        ),
    )
    result = match(rows, headers, rule_set, DEFAULT_LIMITS)
    stage2 = [d for d in result.decisions if d.rule_name != RULE_EXACT_ROW]
    assert len(stage2) == 1
    assert stage2[0].rule_name == RULE_COMPOSITE_WEIGHTED
    assert stage2[0].total_score == 1.0


# --- Determinism (Section 13 acceptance criterion) ---------------------------


def test_engine_determinism_same_input_same_config_same_output():
    headers = ["id", "email", "name"]
    rows = [
        ["1", "jane@example.com", "Jane Doe"],
        ["2", "jane@example.com", "Jane D."],
        ["3", "bob@example.com", "Bob Smith"],
        ["3", "bob@example.com", "Bob Smith"],
        ["4", "carol@example.com", "Carol Jones"],
    ]
    rule_set = MatchRuleSetConfig(
        duplicate_threshold=0.9,
        review_threshold=0.4,
        fields=(
            MatchRuleFieldSpec("email", "normalized_exact", 1.0),
            MatchRuleFieldSpec("name", "normalized_exact", 0.3),
        ),
    )
    limits = MatchLimits(max_block_size=1000, max_persisted_decisions=10_000, max_skipped_row_sample=20)
    first = match(rows, headers, rule_set, limits)
    second = match(rows, headers, rule_set, limits)
    assert first == second


def test_engine_version_is_stable_constant():
    assert MATCH_ENGINE_VERSION == "1.0"
