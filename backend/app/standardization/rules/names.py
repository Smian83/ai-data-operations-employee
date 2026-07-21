"""
Person name / company name standardization (design doc Section 6).
Whitespace + casing only by default -- no structural changes (no "Last,
First" reordering; that changes meaning, not just format, and is
explicitly out of scope). Company names additionally consult an
organization-configured suffix lookup table; there is no built-in
default suffix table, since "correct" canonical suffix form is
organization policy, not a fact.
"""
from __future__ import annotations

from app.standardization.rules.casing import title_case_step
from app.standardization.rules.constants import RULE_COMPANY_SUFFIX_LOOKUP


def standardize_person_name(value: str) -> tuple[str, str | None]:
    return title_case_step(value)


def standardize_company_name(value: str, lookup: dict[str, str]) -> tuple[str, str | None]:
    """lookup is organization StandardizationLookupEntry rows scoped to
    field_type='company_name', keyed lower-case (e.g. {"inc": "Incorporated",
    "inc.": "Incorporated"}). Consulted on the whole trimmed value first
    (exact-match suffix canonicalization is the only supported form here --
    no sub-string/regex rewriting); falls back to title-casing."""
    if value == "":
        return value, None

    lookup_key = value.strip().lower()
    if lookup_key in lookup:
        canonical = lookup[lookup_key]
        if canonical == value:
            return value, None
        return canonical, RULE_COMPANY_SUFFIX_LOOKUP

    return title_case_step(value)
