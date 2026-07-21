"""Internal immutable value objects for the Module 8 matching engine.
Pure dataclasses, no I/O -- mirrors app.standardization.types' shape."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MatchLimits:
    max_block_size: int
    max_persisted_decisions: int
    max_skipped_row_sample: int


@dataclass(frozen=True)
class MatchRuleFieldSpec:
    """One configured field within a resolved MatchRuleSet. `fields` on
    MatchRuleSetConfig below is ordered by (created_at, id) as loaded from
    the database -- the engine's blocking-field tie-break (Section 6:
    "lowest id, i.e. creation order, if weights tie") relies on this
    ordering, not on comparing MatchRuleField.id values directly (id is a
    random uuid4, not a sortable creation-order key; the handler is
    responsible for supplying fields already in true creation order)."""

    column_name: str
    comparison_type: str  # "exact" | "normalized_exact"
    weight: float


@dataclass(frozen=True)
class MatchRuleSetConfig:
    duplicate_threshold: float
    review_threshold: float
    # Ordered by true creation order (see MatchRuleFieldSpec's docstring).
    fields: tuple[MatchRuleFieldSpec, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Decision:
    """One recorded pairwise comparison result, mirroring MatchDecision's
    columns exactly (Section 3/9 of the design doc) plus one
    engine-internal field (`group_index`) the persistence layer consumes
    to wire up MatchDecision.match_group_id -- never itself persisted."""

    record_a_row_index: int
    record_b_row_index: int
    blocking_key: str | None
    rule_name: str
    field_comparisons: dict
    total_score: float
    threshold_used: float
    decision: str  # "duplicate" | "ambiguous"
    confidence_score: float
    reason: str
    rule_version: str
    # Index into MatchResult.groups this decision fed, or None for every
    # 'ambiguous' decision (which never feeds a group -- Section 6).
    group_index: int | None


@dataclass(frozen=True)
class Group:
    canonical_row_index: int
    member_row_indices: tuple[int, ...]
    confidence_score: float

    @property
    def record_count(self) -> int:
        return len(self.member_row_indices)


@dataclass(frozen=True)
class SkippedBlock:
    blocking_key: str
    block_size: int
    sample_row_indices: tuple[int, ...]


@dataclass(frozen=True)
class MatchResult:
    groups: list[Group]
    # Bounded to MatchLimits.max_persisted_decisions -- duplicate_pairs_
    # count/ambiguous_pairs_count/decisions_by_rule below are always the
    # true totals even when this list is capped.
    decisions: list[Decision]
    # Bounded structurally (row_count / MATCH_MAX_BLOCK_SIZE), never
    # separately capped -- see design doc Section 3/11.
    skipped_blocks: list[SkippedBlock]
    row_count: int
    total_comparisons_count: int
    duplicate_group_count: int
    duplicate_pairs_count: int
    ambiguous_pairs_count: int
    skipped_block_count: int
    decisions_by_rule: dict[str, int]
    # Minimum confidence across all groups' confidence_score (1.0 if
    # there were none) -- conservative aggregate, same semantics as
    # app.standardization.engine's confidence_score.
    confidence_score: float
