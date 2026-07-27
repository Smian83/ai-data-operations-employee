"""Module 21 — Central Rule Registry.

This is the authoritative source for all rule keys. Each entry defines the
rule's metadata, default value, validator, and the modules it affects.

RULE_SCHEMA_VERSION is bumped when the registry schema itself changes (new
fields on RuleDefinition, etc.). It is stored on every BusinessRuleSet row
so the resolver can detect schema drift.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable


RULE_SCHEMA_VERSION = "1.0"


@dataclass(frozen=True)
class RuleDefinition:
    """Metadata for one rule in the registry."""

    rule_key: str
    description: str
    value_type: str  # "float", "int", "bool", "str", "list_str", "dict"
    default_value: Any
    validator: Callable[[Any], bool]  # pure Python, validates raw JSON value
    affected_modules: list[str]
    introduced_version: str
    deprecated: bool = False


def _is_float(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _is_bool(v: Any) -> bool:
    return isinstance(v, bool)


def _is_str(v: Any) -> bool:
    return isinstance(v, str)


def _is_list_str(v: Any) -> bool:
    return isinstance(v, list) and all(isinstance(x, str) for x in v)


def _is_dict(v: Any) -> bool:
    return isinstance(v, dict)


def _float_range(lo: float, hi: float) -> Callable[[Any], bool]:
    def _v(v: Any) -> bool:
        return _is_float(v) and lo <= v <= hi
    return _v


def _severity_str(v: Any) -> bool:
    return _is_str(v) and v in ("INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL")


def _cap_target(v: Any) -> bool:
    return _is_str(v) and v in ("lower", "upper", "title", "none")


def _nonneg_int(v: Any) -> bool:
    return _is_int(v) and v >= 0


RULE_REGISTRY: list[RuleDefinition] = [
    # ---- Detection rules ----
    RuleDefinition(
        rule_key="detection.outlier_zscore_threshold",
        description="Z-score threshold above which a numeric value is flagged as an outlier.",
        value_type="float",
        default_value=3.5,
        validator=lambda v: _is_float(v) and v > 0,
        affected_modules=["M14"],
        introduced_version="21.0",
    ),
    RuleDefinition(
        rule_key="detection.missing_value_severity",
        description="Severity level assigned to missing-value issues.",
        value_type="str",
        default_value="MEDIUM",
        validator=_severity_str,
        affected_modules=["M14"],
        introduced_version="21.0",
    ),
    RuleDefinition(
        rule_key="detection.required_columns",
        description="Column names that must be present in the dataset.",
        value_type="list_str",
        default_value=[],
        validator=_is_list_str,
        affected_modules=["M14"],
        introduced_version="21.0",
    ),
    RuleDefinition(
        rule_key="detection.max_duplicate_rate",
        description="Maximum fraction of duplicate rows before the run is flagged.",
        value_type="float",
        default_value=0.05,
        validator=_float_range(0.0, 1.0),
        affected_modules=["M14"],
        introduced_version="21.0",
    ),
    # ---- Remediation rules ----
    RuleDefinition(
        rule_key="remediation.default_capitalization_target",
        description="Default capitalization style applied when no column-level rule exists.",
        value_type="str",
        default_value="none",
        validator=_cap_target,
        affected_modules=["M15"],
        introduced_version="21.0",
    ),
    RuleDefinition(
        rule_key="remediation.date_normalization_format",
        description="Target date format string for date normalization.",
        value_type="str",
        default_value="YYYY-MM-DD",
        validator=_is_str,
        affected_modules=["M15"],
        introduced_version="21.0",
    ),
    RuleDefinition(
        rule_key="remediation.phone_default_country",
        description="Default country code for phone normalization (ISO 3166-1 alpha-2).",
        value_type="str",
        default_value="US",
        validator=_is_str,
        affected_modules=["M15"],
        introduced_version="21.0",
    ),
    RuleDefinition(
        rule_key="remediation.auto_approve_whitespace_fixes",
        description="If true, whitespace-only fixes are auto-approved without human review.",
        value_type="bool",
        default_value=False,
        validator=_is_bool,
        affected_modules=["M15", "M16"],
        introduced_version="21.0",
    ),
    # ---- Validation rules ----
    RuleDefinition(
        rule_key="validation.max_failure_rate",
        description="Maximum fraction of validation failures before the run is flagged.",
        value_type="float",
        default_value=0.10,
        validator=_float_range(0.0, 1.0),
        affected_modules=["M17"],
        introduced_version="21.0",
    ),
    RuleDefinition(
        rule_key="validation.required_pass_rate",
        description="Minimum fraction of validations that must pass.",
        value_type="float",
        default_value=0.80,
        validator=_float_range(0.0, 1.0),
        affected_modules=["M17"],
        introduced_version="21.0",
    ),
    # ---- Quality rules ----
    RuleDefinition(
        rule_key="quality.pass_score_threshold",
        description="Overall score (0–1) at or above which the dataset receives a PASS recommendation.",
        value_type="float",
        default_value=0.85,
        validator=_float_range(0.0, 1.0),
        affected_modules=["M18"],
        introduced_version="21.0",
    ),
    RuleDefinition(
        rule_key="quality.fail_score_threshold",
        description="Overall score (0–1) below which the dataset receives a FAIL recommendation.",
        value_type="float",
        default_value=0.60,
        validator=_float_range(0.0, 1.0),
        affected_modules=["M18"],
        introduced_version="21.0",
    ),
    RuleDefinition(
        rule_key="quality.max_critical_unresolved",
        description="Maximum number of unresolved CRITICAL issues allowed for a PASS.",
        value_type="int",
        default_value=0,
        validator=_nonneg_int,
        affected_modules=["M18"],
        introduced_version="21.0",
    ),
    RuleDefinition(
        rule_key="quality.max_high_unresolved",
        description="Maximum number of unresolved HIGH issues allowed for a PASS.",
        value_type="int",
        default_value=3,
        validator=_nonneg_int,
        affected_modules=["M18"],
        introduced_version="21.0",
    ),
    RuleDefinition(
        rule_key="quality.auto_approve_threshold",
        description="Score at or above which the dataset is auto-approved without manual review.",
        value_type="float",
        default_value=0.95,
        validator=_float_range(0.0, 1.0),
        affected_modules=["M18"],
        introduced_version="21.0",
    ),
    RuleDefinition(
        rule_key="quality.require_manual_review_below",
        description="Score below which manual review is required before export.",
        value_type="float",
        default_value=0.70,
        validator=_float_range(0.0, 1.0),
        affected_modules=["M18"],
        introduced_version="21.0",
    ),
    # ---- Export rules ----
    RuleDefinition(
        rule_key="export.blocked_columns",
        description="Column names that must be excluded from clean exports.",
        value_type="list_str",
        default_value=[],
        validator=_is_list_str,
        affected_modules=["M19"],
        introduced_version="21.0",
    ),
    RuleDefinition(
        rule_key="export.min_quality_score",
        description="Minimum overall quality score (0–1) required for export.",
        value_type="float",
        default_value=0.0,
        validator=_float_range(0.0, 1.0),
        affected_modules=["M19"],
        introduced_version="21.0",
    ),
    RuleDefinition(
        rule_key="export.require_zero_critical",
        description="If true, export is blocked when any CRITICAL issue remains unresolved.",
        value_type="bool",
        default_value=False,
        validator=_is_bool,
        affected_modules=["M19"],
        introduced_version="21.0",
    ),
    # ---- Business validation rules (for business_rule_compliance category) ----
    RuleDefinition(
        rule_key="business.required_column_names",
        description="Column names that must be present in the dataset for business rule compliance.",
        value_type="list_str",
        default_value=[],
        validator=_is_list_str,
        affected_modules=["M18"],
        introduced_version="21.0",
    ),
    RuleDefinition(
        rule_key="business.forbidden_column_values",
        description="Map of column_name -> list of forbidden values for business rule compliance.",
        value_type="dict",
        default_value={},
        validator=_is_dict,
        affected_modules=["M18"],
        introduced_version="21.0",
    ),
    RuleDefinition(
        rule_key="business.min_row_count",
        description="Minimum number of rows required for business rule compliance.",
        value_type="int",
        default_value=0,
        validator=_nonneg_int,
        affected_modules=["M18"],
        introduced_version="21.0",
    ),
    # ---- Report rules ----
    RuleDefinition(
        rule_key="report.include_raw_counts",
        description="If true, raw issue/change counts are included in pipeline reports.",
        value_type="bool",
        default_value=True,
        validator=_is_bool,
        affected_modules=["M20"],
        introduced_version="21.0",
    ),
    RuleDefinition(
        rule_key="report.include_lineage",
        description="If true, full audit lineage is included in pipeline reports.",
        value_type="bool",
        default_value=True,
        validator=_is_bool,
        affected_modules=["M20"],
        introduced_version="21.0",
    ),
]

# Indexed lookup: rule_key -> RuleDefinition
RULE_REGISTRY_BY_KEY: dict[str, RuleDefinition] = {
    r.rule_key: r for r in RULE_REGISTRY
}

# Builtin defaults dict: rule_key -> default_value (for resolver fallback)
BUILTIN_DEFAULTS: dict[str, Any] = {
    r.rule_key: r.default_value for r in RULE_REGISTRY
}

# Rule type mapping: rule_key -> rule_type (threshold/toggle/preference/list/severity_override)
# Derived automatically from value_type for the seed migration.
def _infer_rule_type(rd: RuleDefinition) -> str:
    if rd.value_type == "bool":
        return "toggle"
    if rd.value_type in ("list_str",):
        return "list"
    if rd.value_type == "dict":
        return "severity_override"
    if rd.value_type in ("float", "int"):
        return "threshold"
    return "preference"


RULE_REGISTRY_WITH_TYPES: list[dict] = [
    {
        "rule_key": r.rule_key,
        "rule_type": _infer_rule_type(r),
        "description": r.description,
        "default_value": r.default_value,
        "introduced_version": r.introduced_version,
        "deprecated": r.deprecated,
    }
    for r in RULE_REGISTRY
]
