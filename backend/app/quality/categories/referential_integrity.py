"""Referential Integrity category — always skipped in V1.

This category is a named slot reserved for Module 21 (Business Rules Engine),
which will provide FK metadata for cross-table referential integrity checks.
Until Module 21 wires in its FK metadata store, is_applicable() always
returns False and no finding is ever emitted.

is_applicable() = False means:
  - No QualityFinding row is persisted.
  - category_statuses["referential_integrity"] = "skipped".
  - category_scores entry is absent.
  - category_weights_used entry is absent.
  - The category's weight (10) does not contribute to the denominator of
    the weighted-average overall_score.

evaluate() is defined but always raises NotImplementedError — it is
unreachable while is_applicable() returns False and serves as a structural
guard against accidental calls.

Default weight: 10 (Section 6.1 — listed for future use; never applied in V1).
"""
from __future__ import annotations

from app.quality.types import CategoryEvaluationResult, QualityEngineInput

_CATEGORY = "referential_integrity"
_RULE_VERSION = "1.0"


class ReferentialIntegrityCategory:
    category_name: str = _CATEGORY
    default_weight: int = 10
    category_rule_version: str = _RULE_VERSION

    def is_applicable(self, inputs: QualityEngineInput) -> bool:  # noqa: ARG002
        """Always False in V1 — pending Module 21 FK metadata store."""
        return False

    def evaluate(self, inputs: QualityEngineInput) -> CategoryEvaluationResult:  # noqa: ARG002
        raise NotImplementedError(
            "ReferentialIntegrityCategory.evaluate() must never be called "
            "while is_applicable() returns False. This is a structural guard "
            "against accidental invocation — Module 21 will override this "
            "when FK metadata becomes available."
        )
