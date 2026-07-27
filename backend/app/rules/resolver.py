"""Module 21 — Pure Rule Resolver Engine.

resolve_rules() is the only public function. It accepts pre-loaded ORM row
collections (no DB access here) and returns a frozen ResolvedRuleSet.

Resolution order for each key:
  1. DS-scoped items (data-source-specific active rule set)
  2. Org-wide items (org-wide active rule set)
  3. Global defaults (business_rule_global_defaults table)
  4. BUILTIN_DEFAULTS dict (hardcoded in registry.py)

No I/O, no DB, no side effects. All inputs are plain Python objects or ORM
rows accessed only via attribute access.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.rules.registry import BUILTIN_DEFAULTS, RULE_SCHEMA_VERSION

if TYPE_CHECKING:
    from app.models.business_rule_global_default import BusinessRuleGlobalDefault
    from app.models.business_rule_set import BusinessRuleSet
    from app.models.business_rule_set_item import BusinessRuleSetItem

RESOLVER_VERSION = "1.0.0"


@dataclass(frozen=True)
class ResolvedRuleSet:
    """Immutable result of rule resolution for one pipeline run."""

    organization_id: uuid.UUID
    data_source_id: uuid.UUID | None
    rule_set_id: uuid.UUID | None           # None if no active rule set found
    rule_set_version: int | None
    rule_schema_version: str
    resolver_version: str
    resolved_rules: dict[str, Any]          # rule_key -> resolved value
    resolved_rules_sha256: str              # SHA-256 of canonical JSON


def _sha256_of_rules(resolved: dict[str, Any]) -> str:
    canonical = json.dumps(resolved, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


def resolve_rules(
    organization_id: uuid.UUID,
    data_source_id: uuid.UUID | None,
    global_defaults: "list[BusinessRuleGlobalDefault]",
    ds_scoped_items: "list[BusinessRuleSetItem]",
    org_wide_items: "list[BusinessRuleSetItem]",
    ds_rule_set: "BusinessRuleSet | None",
    org_rule_set: "BusinessRuleSet | None",
) -> ResolvedRuleSet:
    """Resolve all rules for one pipeline run.

    Precedence (highest to lowest):
      ds_scoped_items > org_wide_items > global_defaults > BUILTIN_DEFAULTS

    rule_set_id / rule_set_version: taken from ds_rule_set if present, else
    org_rule_set, else None/None.

    rule_schema_version: from the winning rule set, else RULE_SCHEMA_VERSION.
    """
    # Build lookup maps (rule_key -> value) for each tier
    ds_map: dict[str, Any] = {item.rule_key: item.rule_value for item in ds_scoped_items}
    org_map: dict[str, Any] = {item.rule_key: item.rule_value for item in org_wide_items}
    global_map: dict[str, Any] = {
        gd.rule_key: gd.rule_value for gd in global_defaults
    }

    # Collect all known keys from all tiers + builtins
    all_keys = (
        set(BUILTIN_DEFAULTS)
        | set(global_map)
        | set(org_map)
        | set(ds_map)
    )

    resolved: dict[str, Any] = {}
    for key in all_keys:
        if key in ds_map:
            resolved[key] = ds_map[key]
        elif key in org_map:
            resolved[key] = org_map[key]
        elif key in global_map:
            resolved[key] = global_map[key]
        else:
            resolved[key] = BUILTIN_DEFAULTS[key]

    # Determine winning rule set metadata
    winning_set = ds_rule_set if ds_rule_set is not None else org_rule_set
    rule_set_id = getattr(winning_set, "id", None)
    rule_set_version = getattr(winning_set, "version", None)
    rule_schema_version = (
        getattr(winning_set, "rule_schema_version", RULE_SCHEMA_VERSION)
        if winning_set is not None
        else RULE_SCHEMA_VERSION
    )

    sha256 = _sha256_of_rules(resolved)

    return ResolvedRuleSet(
        organization_id=organization_id,
        data_source_id=data_source_id,
        rule_set_id=rule_set_id,
        rule_set_version=rule_set_version,
        rule_schema_version=rule_schema_version,
        resolver_version=RESOLVER_VERSION,
        resolved_rules=resolved,
        resolved_rules_sha256=sha256,
    )
