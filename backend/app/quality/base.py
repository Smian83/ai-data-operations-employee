"""The QualityCategory interface every category in app.quality.categories
implements.

Parallel to app.validation.base.ValidationRule with the same
"open for extension, closed for modification" discipline:
  - Adding a new category never requires touching engine.py or this file.
  - Only app.quality.registry.QUALITY_RULES is updated.

is_applicable() and evaluate() must be pure functions of their argument —
no I/O, no randomness, no wall-clock/locale dependence. Calling them twice
with identical QualityEngineInput must return identical results. This is
the engine's determinism/repeatability acceptance criterion, verified by
tests/test_quality_repeatability.py.

Always-skipped categories (referential_integrity, business_rule_compliance)
implement is_applicable() as always returning False; their evaluate() is
unreachable code that raises NotImplementedError as a structural guard.
"""
from __future__ import annotations

from typing import Protocol

from app.quality.types import CategoryEvaluationResult, QualityEngineInput


class QualityCategory(Protocol):
    """Protocol every category implementation must satisfy.

    category_name: must be one of app.models.enums.QUALITY_CATEGORIES.
    default_weight: positive integer; weight used in overall score unless
        overridden by QualityThresholdConfig.category_weights. Always-skipped
        categories may carry any non-negative weight (it is never used).
    category_rule_version: independent per-category version string — stored
        on every QualityFinding row produced by this category so two findings
        in the same QualityControlRun can carry different category versions if
        one category was bumped independently. Must be non-empty.
    """

    category_name: str
    default_weight: int
    category_rule_version: str

    def is_applicable(self, inputs: QualityEngineInput) -> bool:
        """Return True if this category can be evaluated for the given inputs.

        Returning False means the category is skipped:
          - No finding is emitted.
          - No score contribution.
          - category_statuses[self.category_name] = "skipped".
          - category_scores entry is absent (not 0.0).
          - category_weights_used entry is absent.

        Must be deterministic: same inputs → same return value every call.
        """
        ...

    def evaluate(self, inputs: QualityEngineInput) -> CategoryEvaluationResult:
        """Evaluate this category and return its score + findings.

        Only called when is_applicable() returns True. Must be a pure function
        of inputs — no side effects, no I/O. score must be in [0.0, 100.0].

        findings may be empty (perfect score, no issues to report). findings
        must not contain any findings for categories other than self.category_name.
        """
        ...
