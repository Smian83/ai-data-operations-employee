"""Internal immutable value objects shared by the standardization
classification step and rule engine. Mirrors app.cleaning.types' shape,
plus the additional fields (field_type, rule_version, per-run config)
Module 7 needs that Module 6 didn't. Pure dataclasses, no I/O."""
from dataclasses import dataclass, field


@dataclass(frozen=True)
class StandardizationLimits:
    max_persisted_changes: int


@dataclass(frozen=True)
class StandardizationConfig:
    """Organization-configured context that disambiguates otherwise-
    ambiguous rules (Section 6 of the design doc). Every field defaults to
    None/unset, meaning "no configuration supplied" -- the engine's
    documented behavior for an unresolved ambiguity is to leave the value
    untouched, never to guess a default on its own."""

    default_country: str | None = None  # ISO 3166-1 alpha-2, e.g. "US"
    default_currency: str | None = None  # ISO 4217, e.g. "USD"
    date_output_format: str | None = None  # e.g. "%m/%d/%Y"; None = ISO-8601
    # (true_form, false_form), e.g. ("Yes", "No"); None = "true"/"false"
    boolean_output_form: tuple[str, str] | None = None
    numeric_locale: str | None = None  # "us" (comma=thousands) or "eu" (dot=thousands)


@dataclass(frozen=True)
class LookupTables:
    """Resolved lookup tables for one standardization run: organization
    entries merged over built-in defaults, organization entries always
    winning for a duplicate key (Section 6). Keys are already
    case-folded/trimmed by the loader. `scoped[field_type]` is consulted
    by that field type's own rule; `global_` (field_type=NULL entries) is
    consulted by the generic abbreviation pass."""

    scoped: dict[str, dict[str, str]] = field(default_factory=dict)
    global_: dict[str, str] = field(default_factory=dict)

    def for_field_type(self, field_type: str) -> dict[str, str]:
        return self.scoped.get(field_type, {})


@dataclass(frozen=True)
class Change:
    """One recorded, deterministic cell-level standardization."""

    row_index: int
    column_name: str
    field_type: str
    original_value: str
    standardized_value: str
    rule_name: str
    rule_version: str
    reason: str
    confidence: float


@dataclass(frozen=True)
class StandardizationResult:
    standardized_rows: list[list[str]]
    # Bounded to StandardizationLimits.max_persisted_changes --
    # total_changes_count below is always the true total even when this
    # list is capped.
    changes: list[Change]
    total_changes_count: int
    changes_by_rule: dict[str, int]
    # Minimum confidence across all applied changes (1.0 if there were
    # none) -- a conservative aggregate, not an average, same semantics
    # as app.cleaning.engine's confidence_score.
    confidence_score: float
