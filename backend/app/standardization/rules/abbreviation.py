"""
Generic, field-type-agnostic abbreviation lookup pass (design doc
Section 6). Driven entirely by organization-supplied
StandardizationLookupEntry rows with field_type=NULL -- there is no
built-in default table for this generic case, since "common" is
context- and organization-specific. Exact-match on the whole trimmed
value only (no token-wise or substring rewriting, to keep behavior
simple and predictable) -- applied by the engine as a final pass, after
a field's own type-specific rule(s), only for "free text" field types
(see app.standardization.engine).
"""
from __future__ import annotations

from app.standardization.rules.constants import RULE_ABBREVIATION_LOOKUP


def apply_generic_abbreviation_lookup(value: str, lookup: dict[str, str]) -> tuple[str, str | None]:
    if value == "":
        return value, None
    key = value.strip().lower()
    if key not in lookup:
        return value, None
    canonical = lookup[key]
    if canonical == value:
        return value, None
    return canonical, RULE_ABBREVIATION_LOOKUP
