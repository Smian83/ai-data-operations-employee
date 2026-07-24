"""
Pure validity predicates reused across app.detection.rules -- deterministic,
no I/O, no AI/ML. Deliberately Module 14's own copies rather than importing
app.standardization.rules' functions directly: those are *standardize*
(transform-toward-canonical-form) functions that return "unchanged" both
when a value is already canonical AND when it is unparseable, so they
cannot answer "is this valid" on their own. This mirrors the precedent
app.standardization.rules.temporal's own docstring already set for not
cross-importing app.cleaning's date coercion -- each module owns the pure
functions its own determinism guarantee depends on. Where a genuine
third-party validator already exists (phonenumbers), it is called
directly, the same way app.standardization.rules.contact does.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

import phonenumbers

_THOUSANDS_COMMA_RE = re.compile(r"-?\d{1,3}(,\d{3})+")

# Same conservative "exactly one '@', both parts non-empty" syntactic check
# app.standardization.rules.contact.standardize_email already applies before
# ever touching a value -- reused here as a genuine validity predicate
# rather than a pre-check before a rewrite.
def is_valid_email_syntax(value: str) -> bool:
    parts = value.split("@")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return False
    domain = parts[1]
    return "." in domain and not domain.startswith(".") and not domain.endswith(".")


# Region-agnostic on purpose: Module 14 has no per-row country context
# (unlike app.standardization.rules.contact.standardize_phone, which
# receives an already-resolved country). A number can only be
# deterministically validated here when it is written in explicit
# international form (a leading '+') -- see this function's own callers
# for how that limitation is documented to the operator.
def is_valid_phone_international(value: str) -> bool:
    if not value.startswith("+"):
        return False
    try:
        parsed = phonenumbers.parse(value, None)
    except phonenumbers.NumberParseException:
        return False
    return phonenumbers.is_valid_number(parsed)


# Same ISO-8601-via-fromisoformat approach app.cleaning.rules._coerce_date
# and app.standardization.rules.temporal.standardize_date both already
# use -- kept as Module 14's own copy for the identical
# independent-versioning reason app.standardization.rules.temporal's
# docstring gives for not importing app.cleaning's.
def is_valid_iso_date(value: str) -> bool:
    stripped = value.strip()
    try:
        datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        return True
    except ValueError:
        pass
    try:
        date.fromisoformat(stripped)
        return True
    except ValueError:
        return False


def is_valid_numeric(value: str) -> bool:
    """A conservative, locale-agnostic numeric check: strips a single
    optional thousands-comma-grouping (\\d{1,3}(,\\d{3})+) before parsing,
    since that is the one unambiguous case app.standardization.rules.
    values.standardize_numeric also treats as safe regardless of locale;
    anything else is parsed exactly as written. Never guesses a locale."""
    stripped = value.strip()
    if not stripped:
        return False
    candidate = stripped
    if "," in candidate and "." not in candidate:
        if _THOUSANDS_COMMA_RE.fullmatch(candidate):
            candidate = candidate.replace(",", "")
    try:
        parsed = Decimal(candidate)
    except InvalidOperation:
        return False
    return parsed.is_finite()


# Canonical boolean token families -- Module 14's own copy of the same
# {"true","yes","y","1"} / {"false","no","n","0"} sets app.cleaning.rules
# and app.standardization.rules.values each already independently define
# (same "each module owns its own copy" precedent). Grouped into named
# families (rather than one flat true/false set) so
# app.detection.rules.boolean can tell "true"/"false" spelling apart from
# "1"/"0" apart from "yes"/"no" -- the exact distinction
# boolean_inconsistency needs to detect a column mixing representations.
BOOLEAN_TOKEN_FAMILIES: dict[str, dict[str, bool]] = {
    "true_false": {"true": True, "false": False},
    "yes_no": {"yes": True, "no": False},
    "y_n": {"y": True, "n": False},
    "one_zero": {"1": True, "0": False},
}


def classify_boolean_token(value: str) -> tuple[str | None, bool | None]:
    """Returns (family_name, parsed_bool) for a recognized boolean token
    (case-insensitive, trimmed), or (None, None) if value is not a
    recognized boolean representation at all."""
    lowered = value.strip().casefold()
    for family, tokens in BOOLEAN_TOKEN_FAMILIES.items():
        if lowered in tokens:
            return family, tokens[lowered]
    return None, None
