"""Business Rule Compliance category — always skipped in V1.

This category is a named slot reserved for Module 21 (Business Rules Engine),
which will provide operator-authored business rules (minimum row count, column
regex, column value sets, custom SQL, etc.). Until Module 21 wires in its
rule store, is_applicable() always returns False and no finding is ever emitted.

See Section 5 of the architecture document for the full deferral rationale.
In summary: introducing a generic quality_business_rules table in Module 18
would create a competing architecture that Module 21 must either duplicate or
reconcile. The slot exists so Module 21 can activate it without requiring any
change to app.quality.registry or engine.py.

is_applicable() = False means:
  - No QualityFinding row is persisted.
  - category_statuses["business_rule_compliance"] = "skipped".
  - category_scores entry is absent.
  - category_weights_used entry is absent.
  - The category's weight (15) does not contribute to the denominator of
    the weighted-average overall_score.

Default weight: 15 (Section 6.1 — listed for future use; never applied in V1).
"""
from __future__ import annotations

from app.quality.types import CategoryEvaluationResult, QualityEngineInput

_CATEGORY = "business_rule_compliance"
_RULE_VERSION = "1.0"


class BusinessRuleComplianceCategory:
    category_name: str = _CATEGORY
    default_weight: int = 15
    category_rule_version: str = _RULE_VERSION

    def is_applicable(self, inputs: QualityEngineInput) -> bool:  # noqa: ARG002
        """Always False in V1 — pending Module 21 business rule store."""
        return False

    def evaluate(self, inputs: QualityEngineInput) -> CategoryEvaluationResult:  # noqa: ARG002
        raise NotImplementedError(
            "BusinessRuleComplianceCategory.evaluate() must never be called "
            "while is_applicable() returns False. This is a structural guard "
            "against accidental invocation — Module 21 will activate this "
            "when its business rule store is available."
        )
