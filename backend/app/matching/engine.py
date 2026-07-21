"""
Module 8: orchestrates the deterministic matching/deduplication pipeline
described in docs/module-8-data-matching-deduplication-design.md Sections
1, 6, 7, 8. `match()` is pure -- no I/O, no randomness, no AI/ML, no
fuzzy/phonetic/approximate comparison -- so it always produces an
identical MatchResult for identical input, rule set, and organization
configuration (this engine's determinism acceptance criterion), and is
naturally idempotent: running it again on the same input always yields
the same groups, decisions, scores, blocking identifiers, and
skipped-block audit results. MatchHandler (app.worker.handlers.matching)
is the only caller; all persistence happens there, not here.
"""
from __future__ import annotations

from app.matching.blocking import build_blocks, select_blocking_field
from app.matching.canonical import select_canonical
from app.matching.clustering import UnionFind
from app.matching.comparison import compare_pair
from app.matching.types import Decision, Group, MatchLimits, MatchResult, MatchRuleSetConfig, SkippedBlock

# Bumped whenever any function's OUTPUT could change for existing input.
MATCH_ENGINE_VERSION = "1.0"

# Fixed rule-name registry (Section 7) -- resolved from the count of
# configured MatchRuleField rows at comparison time, not stored per-field.
RULE_EXACT_ROW = "exact_row_match"
RULE_NORMALIZED_EXACT = "normalized_exact_match"
RULE_COMPOSITE_WEIGHTED = "composite_weighted_match"


def match(
    rows: list[list[str]],
    headers: list[str],
    rule_set: MatchRuleSetConfig | None,
    limits: MatchLimits,
) -> MatchResult:
    n = len(rows)
    uf = UnionFind(n)
    decisions: list[Decision] = []
    decisions_by_rule: dict[str, int] = {}
    duplicate_pairs_count = 0
    ambiguous_pairs_count = 0
    total_comparisons_count = 0
    skipped_blocks: list[SkippedBlock] = []
    # (a, b, confidence) for every unioned ('duplicate') edge, used after
    # clustering to compute each final group's minimum confidence.
    duplicate_edges: list[tuple[int, int, float]] = []
    # Provisional decisions recorded before clustering resolves; group_index
    # is filled in afterward for every 'duplicate' decision.
    pending_duplicate_edge_positions: list[int] = []  # indices into `decisions`

    def record_decision(
        a: int,
        b: int,
        rule_name: str,
        blocking_key: str | None,
        field_comparisons: dict,
        total_score: float,
        threshold_used: float,
        decision_label: str,
        confidence: float,
        reason: str,
    ) -> None:
        nonlocal duplicate_pairs_count, ambiguous_pairs_count
        decisions_by_rule[rule_name] = decisions_by_rule.get(rule_name, 0) + 1
        if decision_label == "duplicate":
            duplicate_pairs_count += 1
        else:
            ambiguous_pairs_count += 1
        if len(decisions) < limits.max_persisted_decisions:
            decisions.append(
                Decision(
                    record_a_row_index=a,
                    record_b_row_index=b,
                    blocking_key=blocking_key,
                    rule_name=rule_name,
                    field_comparisons=field_comparisons,
                    total_score=total_score,
                    threshold_used=threshold_used,
                    decision=decision_label,
                    confidence_score=confidence,
                    reason=reason,
                    rule_version=MATCH_ENGINE_VERSION,
                    group_index=None,  # filled in after clustering below
                )
            )
            if decision_label == "duplicate":
                pending_duplicate_edge_positions.append(len(decisions) - 1)

    # ---------------------------------------------------------------
    # Stage 1: exact duplicate detection (always runs, no configuration
    # needed). Rows sharing an identical full-row key are chain-linked
    # (row[i], row[i+1]) rather than fully pairwise-connected -- O(group
    # size) edges are sufficient for union-find connectivity, keeping
    # this stage the cheap O(row_count) dictionary pass the design
    # promises rather than an O(size^2) blowup for large duplicate
    # groups.
    # ---------------------------------------------------------------
    exact_groups: dict[tuple[str, ...], list[int]] = {}
    for idx, row in enumerate(rows):
        exact_groups.setdefault(tuple(row), []).append(idx)

    # representative row_index (lowest in its group) for every row that
    # belongs to a Stage-1 group of size >= 2.
    stage1_representative_for: dict[int, int] = {}
    for indices in exact_groups.values():
        if len(indices) < 2:
            continue
        indices = sorted(indices)
        rep = indices[0]
        for i in indices:
            stage1_representative_for[i] = rep
        group_size = len(indices)
        for i in range(group_size - 1):
            a, b = indices[i], indices[i + 1]
            uf.union(a, b)
            duplicate_edges.append((a, b, 1.0))
            total_comparisons_count += 1
            field_comparisons = {
                header: {
                    "value_a": rows[a][col],
                    "value_b": rows[b][col],
                    "matched": True,
                    "weight": 1.0,
                    "contribution": 1.0,
                }
                for col, header in enumerate(headers)
            }
            record_decision(
                a,
                b,
                RULE_EXACT_ROW,
                None,
                field_comparisons,
                1.0,
                1.0,
                "duplicate",
                1.0,
                f"identical row (Stage 1 exact match, chain-linked within a "
                f"{group_size}-row duplicate group)",
            )

    # ---------------------------------------------------------------
    # Stage 2: composite/normalized matching -- runs only if an active
    # MatchRuleSet with at least one configured field was resolved.
    # ---------------------------------------------------------------
    if rule_set is not None and rule_set.fields:
        candidate_indices: list[int] = []
        seen_representatives: set[int] = set()
        for idx in range(n):
            rep = stage1_representative_for.get(idx)
            if rep is None:
                candidate_indices.append(idx)
            elif rep not in seen_representatives:
                candidate_indices.append(rep)
                seen_representatives.add(rep)
        candidate_indices.sort()

        blocking_field = select_blocking_field(rule_set.fields)
        blocks = build_blocks(candidate_indices, rows, headers, blocking_field)
        rule_name = (
            RULE_NORMALIZED_EXACT if len(rule_set.fields) == 1 else RULE_COMPOSITE_WEIGHTED
        )

        for key in sorted(blocks.keys()):
            members = sorted(blocks[key])
            if len(members) > limits.max_block_size:
                skipped_blocks.append(
                    SkippedBlock(
                        blocking_key=key,
                        block_size=len(members),
                        sample_row_indices=tuple(members[: limits.max_skipped_row_sample]),
                    )
                )
                continue
            for i in range(len(members)):
                for j in range(i + 1, len(members)):
                    a, b = members[i], members[j]
                    field_comparisons, total_score = compare_pair(
                        rows[a], rows[b], headers, rule_set.fields
                    )
                    total_comparisons_count += 1
                    matched_count = sum(1 for fc in field_comparisons.values() if fc["matched"])
                    total_fields = len(field_comparisons)
                    if total_score >= rule_set.duplicate_threshold:
                        decision_label = "duplicate"
                        uf.union(a, b)
                        duplicate_edges.append((a, b, total_score))
                        reason = (
                            f"{matched_count} of {total_fields} configured fields matched "
                            f"(score {total_score:.4f} >= threshold "
                            f"{rule_set.duplicate_threshold:.4f})"
                        )
                    elif total_score >= rule_set.review_threshold:
                        decision_label = "ambiguous"
                        reason = (
                            f"{matched_count} of {total_fields} configured fields matched "
                            f"(score {total_score:.4f} is between the review threshold "
                            f"{rule_set.review_threshold:.4f} and the duplicate threshold "
                            f"{rule_set.duplicate_threshold:.4f}) -- flagged for manual "
                            "review, not grouped"
                        )
                    else:
                        # Below the review threshold: not persisted at
                        # all (Section 6/13) -- total_comparisons_count
                        # above still reflects the true number of pairs
                        # evaluated.
                        continue
                    record_decision(
                        a,
                        b,
                        rule_name,
                        key,
                        field_comparisons,
                        total_score,
                        rule_set.duplicate_threshold,
                        decision_label,
                        total_score,  # confidence == total_score (Section 7)
                        reason,
                    )

    # ---------------------------------------------------------------
    # Stage 3: clustering. Every 'duplicate'-decision edge (both Stage-1
    # chain edges and Stage-2 duplicate edges) is already unioned above;
    # 'ambiguous' decisions were never unioned. Connected components of
    # size >= 2 become MatchGroups; a Stage-1 group's members are always
    # part of the same component as its representative, since the chain
    # edges already union them.
    # ---------------------------------------------------------------
    members_by_root = uf.components()
    root_to_group_index: dict[int, int] = {}
    groups: list[Group] = []
    for root, members in sorted(members_by_root.items(), key=lambda kv: min(kv[1])):
        if len(members) < 2:
            continue
        root_to_group_index[root] = len(groups)
        groups.append(
            Group(
                canonical_row_index=select_canonical(members),
                member_row_indices=tuple(sorted(members)),
                confidence_score=1.0,  # overwritten below
            )
        )

    confidences_by_root: dict[int, list[float]] = {}
    for a, b, confidence in duplicate_edges:
        root = uf.find(a)
        confidences_by_root.setdefault(root, []).append(confidence)

    groups = [
        Group(
            canonical_row_index=g.canonical_row_index,
            member_row_indices=g.member_row_indices,
            confidence_score=min(
                confidences_by_root.get(uf.find(g.canonical_row_index), [1.0])
            ),
        )
        for g in groups
    ]

    # Wire up group_index on every persisted 'duplicate' decision (never
    # persisted directly on MatchDecision -- MatchHandler uses this only
    # to resolve which MatchGroup.id to write to match_group_id).
    decisions = list(decisions)
    for pos in pending_duplicate_edge_positions:
        d = decisions[pos]
        root = uf.find(d.record_a_row_index)
        group_index = root_to_group_index.get(root)
        decisions[pos] = Decision(
            record_a_row_index=d.record_a_row_index,
            record_b_row_index=d.record_b_row_index,
            blocking_key=d.blocking_key,
            rule_name=d.rule_name,
            field_comparisons=d.field_comparisons,
            total_score=d.total_score,
            threshold_used=d.threshold_used,
            decision=d.decision,
            confidence_score=d.confidence_score,
            reason=d.reason,
            rule_version=d.rule_version,
            group_index=group_index,
        )

    overall_confidence = min((g.confidence_score for g in groups), default=1.0)

    return MatchResult(
        groups=groups,
        decisions=decisions,
        skipped_blocks=skipped_blocks,
        row_count=n,
        total_comparisons_count=total_comparisons_count,
        duplicate_group_count=len(groups),
        duplicate_pairs_count=duplicate_pairs_count,
        ambiguous_pairs_count=ambiguous_pairs_count,
        skipped_block_count=len(skipped_blocks),
        decisions_by_rule=decisions_by_rule,
        confidence_score=overall_confidence,
    )
