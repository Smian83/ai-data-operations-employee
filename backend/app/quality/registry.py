"""QUALITY_RULES: all registered quality categories in QUALITY_CATEGORIES
declaration order (Section 6.1 of the architecture document).

Adding a new category is exactly two steps, same as app.validation.registry:
  1. Write the category class in app.quality.categories (implementing
     app.quality.base.QualityCategory).
  2. Add one line to the QUALITY_RULES tuple below.
Nothing in app.quality.engine ever needs to change.

Import-time assertions enforce all structural invariants before any code can
call quality_control():
  1. len(QUALITY_RULES) == 8 == len(QUALITY_CATEGORIES)
  2. All category_names are unique.
  3. All category_names are in QUALITY_CATEGORIES.
  4. All default_weights are non-negative.
  5. Active categories (not always-skipped) have default_weight > 0.
  6. All category_rule_version strings are non-empty.
  7. QUALITY_RULES order matches QUALITY_CATEGORIES declaration order.

O(1) category_name → rule lookup via _RULES_BY_CATEGORY.
"""
from app.models.enums import QUALITY_CATEGORIES
from app.quality.base import QualityCategory
from app.quality.categories.business_rule_compliance import (
    BusinessRuleComplianceCategory,
)
from app.quality.categories.completeness import CompletenessCategory
from app.quality.categories.consistency import ConsistencyCategory
from app.quality.categories.referential_integrity import ReferentialIntegrityCategory
from app.quality.categories.uniqueness import UniquenessCategory
from app.quality.categories.unresolved_risk import UnresolvedRiskCategory
from app.quality.categories.validation_coverage import ValidationCoverageCategory
from app.quality.categories.validity import ValidityCategory

# Sections 5.1 and 6.1: these two categories are permanently skipped in V1.
_ALWAYS_SKIPPED: frozenset[str] = frozenset({
    "referential_integrity",
    "business_rule_compliance",
})

QUALITY_RULES: tuple[QualityCategory, ...] = (
    CompletenessCategory(),
    UniquenessCategory(),
    ValidityCategory(),
    ConsistencyCategory(),
    ReferentialIntegrityCategory(),       # always skips in V1 — no finding emitted
    BusinessRuleComplianceCategory(),     # always skips in V1 — pending Module 21
    UnresolvedRiskCategory(),
    ValidationCoverageCategory(),
)

# ── Integrity check 1: explicit count guard ──────────────────────────────────
assert len(QUALITY_RULES) == len(QUALITY_CATEGORIES) == 8, (
    f"QUALITY_RULES must have exactly 8 entries matching QUALITY_CATEGORIES, "
    f"got {len(QUALITY_RULES)} rules and {len(QUALITY_CATEGORIES)} categories"
)

# ── Integrity check 2: unique category_names ─────────────────────────────────
_category_names = [rule.category_name for rule in QUALITY_RULES]
assert len(_category_names) == len(set(_category_names)), (
    "two or more registered quality categories share a category_name — "
    "each entry in QUALITY_RULES must have a distinct category_name"
)

# ── Integrity check 3: all category_names in QUALITY_CATEGORIES ──────────────
_registered_names = set(_category_names)
for _name in QUALITY_CATEGORIES:
    assert _name in _registered_names, (
        f"QUALITY_CATEGORY {_name!r} has no corresponding rule in QUALITY_RULES — "
        f"add one or correct the name"
    )

# ── Integrity check 4: all default_weights non-negative ──────────────────────
for _rule in QUALITY_RULES:
    assert _rule.default_weight >= 0, (
        f"category {_rule.category_name!r} has negative default_weight "
        f"{_rule.default_weight} — weights must be non-negative"
    )

# ── Integrity check 5: active categories have weight > 0 ─────────────────────
for _rule in QUALITY_RULES:
    if _rule.category_name not in _ALWAYS_SKIPPED:
        assert _rule.default_weight > 0, (
            f"active category {_rule.category_name!r} has default_weight == 0 — "
            f"only always-skipped categories (referential_integrity, "
            f"business_rule_compliance) may have weight 0"
        )

# ── Integrity check 6: non-empty category_rule_version per rule ──────────────
for _rule in QUALITY_RULES:
    assert _rule.category_rule_version, (
        f"category {_rule.category_name!r} has an empty category_rule_version — "
        f"a non-empty version string is required per rule (mirrors "
        f"ValidationRule.rule_version discipline)"
    )

# ── Integrity check 7: order matches QUALITY_CATEGORIES declaration order ─────
for _i, (_rule, _expected_name) in enumerate(zip(QUALITY_RULES, QUALITY_CATEGORIES)):
    assert _rule.category_name == _expected_name, (
        f"QUALITY_RULES[{_i}].category_name = {_rule.category_name!r} does not "
        f"match QUALITY_CATEGORIES[{_i}] = {_expected_name!r} — "
        f"QUALITY_RULES must be declared in the same order as QUALITY_CATEGORIES"
    )

# ── O(1) category_name → rule lookup ─────────────────────────────────────────
_RULES_BY_CATEGORY: dict[str, QualityCategory] = {
    rule.category_name: rule for rule in QUALITY_RULES
}


def get_rule_for_category(category_name: str) -> QualityCategory | None:
    """Return the QualityCategory for the given category name, or None.

    None should never occur in a well-formed system (all 8 categories are
    covered by the assertions above), but callers can guard defensively.
    """
    return _RULES_BY_CATEGORY.get(category_name)
