"""
Postal address / city / state-province / country / postal-code
standardization (design doc Section 6). Every rule here is light-touch
only, matching Module 6's own "casing/format normalization... no
aggressive rewriting" principle: no structural address parsing, no
canonical-city-name lookup, no USPS CASS-style certification or
geocoding -- all explicitly out of scope. State/province and postal-code
normalization are only applied when a country is resolvable for that
row, to avoid exactly the kind of collision an unqualified abbreviation
creates (e.g. "GA" is both a US state and, unrelated, ambiguous without
a known country).
"""
from __future__ import annotations

import re

from app.standardization.rules.casing import title_case_step
from app.standardization.rules.constants import (
    RULE_ADDRESS_ABBREVIATION,
    RULE_COUNTRY_ISO_NORMALIZE,
    RULE_POSTAL_CODE_FORMAT,
    RULE_STATE_PROVINCE_ABBREVIATION,
)
from app.standardization.rules.lookups import (
    DEFAULT_CA_PROVINCE_ABBREVIATIONS,
    DEFAULT_COUNTRY_NAME_VARIANTS,
    DEFAULT_US_STATE_ABBREVIATIONS,
)

_NON_ALNUM_RE = re.compile(r"[^A-Za-z0-9]")
_US_ZIP_RE = re.compile(r"^\d{5}(-\d{4})?$")
_CA_POSTAL_RE = re.compile(r"^[A-Za-z]\d[A-Za-z]\s?\d[A-Za-z]\d$")


def expand_address_abbreviations(value: str, lookup: dict[str, str]) -> tuple[str, str | None]:
    """Token-wise: each whitespace-separated token is checked (lower-case,
    trailing period stripped for the lookup key only) against the merged
    lookup (organization entries already merged over
    DEFAULT_ADDRESS_ABBREVIATIONS by the caller)."""
    if value == "":
        return value, None
    tokens = value.split(" ")
    changed = False
    new_tokens = []
    for token in tokens:
        key = token.lower()
        if key in lookup:
            new_tokens.append(lookup[key])
            changed = True
        else:
            new_tokens.append(token)
    if not changed:
        return value, None
    return " ".join(new_tokens), RULE_ADDRESS_ABBREVIATION


def standardize_city(value: str) -> tuple[str, str | None]:
    return title_case_step(value)


def standardize_country(value: str, lookup: dict[str, str]) -> tuple[str, str | None]:
    """lookup is organization entries already merged over
    DEFAULT_COUNTRY_NAME_VARIANTS, organization entries winning ties."""
    if value == "":
        return value, None
    key = value.strip().lower()
    merged = {**DEFAULT_COUNTRY_NAME_VARIANTS, **lookup}
    if key not in merged:
        return value, None
    canonical = merged[key]
    if canonical == value:
        return value, None
    return canonical, RULE_COUNTRY_ISO_NORMALIZE


def resolve_country_code(value: str, lookup: dict[str, str]) -> str | None:
    """Best-effort resolution of an ISO code from a (possibly
    already-standardized) country cell, for use as `country` context by
    phone/state/postal_code rules. Returns None if unresolvable --
    callers must never guess past that."""
    if value == "":
        return None
    merged = {**DEFAULT_COUNTRY_NAME_VARIANTS, **lookup}
    key = value.strip().lower()
    if key in merged:
        return merged[key]
    if len(value.strip()) == 2 and value.strip().isalpha():
        return value.strip().upper()
    return None


def standardize_state_province(value: str, country: str | None) -> tuple[str, str | None]:
    if value == "" or country not in ("US", "CA"):
        return value, None
    table = DEFAULT_US_STATE_ABBREVIATIONS if country == "US" else DEFAULT_CA_PROVINCE_ABBREVIATIONS
    key = value.strip().lower()
    if key not in table:
        return value, None
    canonical = table[key]
    if canonical == value:
        return value, None
    return canonical, RULE_STATE_PROVINCE_ABBREVIATION


def standardize_postal_code(value: str, country: str | None) -> tuple[str, str | None]:
    if value == "" or country not in ("US", "CA"):
        return value, None

    if country == "US":
        stripped = _NON_ALNUM_RE.sub("", value)
        if not stripped.isdigit() or len(stripped) not in (5, 9):
            return value, None
        canonical = stripped if len(stripped) == 5 else f"{stripped[:5]}-{stripped[5:]}"
        if canonical == value:
            return value, None
        return canonical, RULE_POSTAL_CODE_FORMAT

    # CA
    compact = _NON_ALNUM_RE.sub("", value).upper()
    if len(compact) != 6 or not _CA_POSTAL_RE.match(f"{compact[:3]} {compact[3:]}"):
        return value, None
    canonical = f"{compact[:3]} {compact[3:]}"
    if canonical == value:
        return value, None
    return canonical, RULE_POSTAL_CODE_FORMAT
