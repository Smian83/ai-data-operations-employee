"""
Deterministic casing primitives shared by several field-type rules.
Deliberately NOT locale-aware "smart" title-casing (no special-casing of
"McDonald", "O'Brien", or lower-case connector words like "de"/"van") --
a named scope limit (design doc Section 11), not an oversight. Splits
only on already-collapsed single spaces (trim_whitespace runs before any
casing rule in the engine's pipeline), so this never has to reason about
irregular internal whitespace itself.
"""
from __future__ import annotations


def title_case(value: str) -> str:
    """Capitalize the first character of each space-separated word,
    lower-case the rest. Splitting on plain whitespace (rather than
    str.title()'s word-boundary detection) avoids str.title()'s known
    apostrophe bug (e.g. "don't".title() == "Don'T")."""
    return " ".join(
        (word[:1].upper() + word[1:].lower()) if word else word for word in value.split(" ")
    )


def upper_case(value: str) -> str:
    return value.upper()


def lower_case(value: str) -> str:
    return value.lower()


def title_case_step(value: str) -> tuple[str, str | None]:
    """Wraps title_case() with the standard empty/no-op-guarded
    (value, rule_name_or_None) contract every other rule function in this
    package uses -- the shared primitive for every field type that wants
    "just title-case the whole value" (city, postal_address's second
    step, person_name, and company_name's casing fallback)."""
    from app.standardization.rules.constants import RULE_TITLE_CASE

    if value == "":
        return value, None
    cased = title_case(value)
    if cased == value:
        return value, None
    return cased, RULE_TITLE_CASE
