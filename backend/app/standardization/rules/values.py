"""
Boolean / numeric / currency standardization (design doc Section 6).
Every rule here leaves genuinely ambiguous input untouched rather than
guessing -- see standardize_numeric's docstring for the exact,
deliberately conservative disambiguation rules.
"""
from __future__ import annotations

import re

from app.standardization.rules.constants import (
    RULE_BOOLEAN_FORMAT_NORMALIZE,
    RULE_CURRENCY_FORMAT_NORMALIZE,
    RULE_NUMERIC_FORMAT_NORMALIZE,
)
from app.standardization.rules.lookups import DEFAULT_CURRENCY_SYMBOL_MAP

_BOOLEAN_TRUE = {"true", "yes", "y", "1"}
_BOOLEAN_FALSE = {"false", "no", "n", "0"}

_THOUSANDS_COMMA_RE = re.compile(r"^-?\d{1,3}(,\d{3})+$")
_THOUSANDS_DOT_RE = re.compile(r"^-?\d{1,3}(\.\d{3})+$")
_TRAILING_COMMA_DECIMAL_RE = re.compile(r"^-?\d+,\d{1,2}$")


def standardize_boolean(value: str, output_form: tuple[str, str] | None) -> tuple[str, str | None]:
    if value == "":
        return value, None
    lowered = value.strip().casefold()
    true_form, false_form = output_form if output_form else ("true", "false")
    if lowered in _BOOLEAN_TRUE:
        canonical = true_form
    elif lowered in _BOOLEAN_FALSE:
        canonical = false_form
    else:
        return value, None
    if canonical == value:
        return value, None
    return canonical, RULE_BOOLEAN_FORMAT_NORMALIZE


def standardize_numeric(value: str, locale: str | None) -> tuple[str, str | None]:
    """locale is StandardizationConfig.numeric_locale: "us" (comma =
    thousands, dot = decimal), "eu" (dot = thousands, comma = decimal),
    or None. Disambiguation rules, most-to-least certain:
      - both ',' and '.' present: whichever appears LAST is the decimal
        separator (unambiguous regardless of locale -- this is how every
        real mixed-separator number is written in both conventions).
      - only ',' present: safe to strip as thousands-grouping if it
        matches the standard \\d{1,3}(,\\d{3})+ shape; otherwise only
        treated as a decimal separator if locale == "eu" and the shape
        matches a plausible decimal (\\d+,\\d{1,2}); otherwise left
        untouched -- genuinely ambiguous.
      - only '.' present: assumed to already be the canonical decimal
        form (the near-universal convention for machine-oriented data)
        unless locale == "eu" and it matches the standard thousands-
        grouping shape, in which case the dots are stripped.
      - neither present: left as-is."""
    if value == "":
        return value, None
    stripped = value.strip()
    has_comma = "," in stripped
    has_dot = "." in stripped
    canonical: str | None

    if has_comma and has_dot:
        canonical = (
            stripped.replace(",", "")
            if stripped.rfind(".") > stripped.rfind(",")
            else stripped.replace(".", "").replace(",", ".")
        )
    elif has_comma:
        if _THOUSANDS_COMMA_RE.match(stripped):
            canonical = stripped.replace(",", "")
        elif locale == "eu" and _TRAILING_COMMA_DECIMAL_RE.match(stripped):
            canonical = stripped.replace(",", ".")
        else:
            return value, None
    elif has_dot:
        canonical = stripped.replace(".", "") if (locale == "eu" and _THOUSANDS_DOT_RE.match(stripped)) else stripped
    else:
        canonical = stripped

    if canonical == value:
        return value, None
    return canonical, RULE_NUMERIC_FORMAT_NORMALIZE


def standardize_currency(
    value: str, default_currency: str | None, locale: str | None
) -> tuple[str, str | None]:
    """Canonical output form: "<amount> <ISO4217code>". Unambiguous
    symbols (euro/pound/yen) resolve to their ISO code directly; the
    ambiguous '$' resolves only via default_currency (never guessed).
    A value with no recognized symbol at all is left untouched -- this
    rule only reformats currency it can positively identify."""
    if value == "":
        return value, None
    stripped = value.strip()

    code: str | None = None
    numeric_part = stripped
    for symbol, iso in DEFAULT_CURRENCY_SYMBOL_MAP.items():
        if symbol in stripped:
            code = iso
            numeric_part = stripped.replace(symbol, "").strip()
            break

    if code is None and "$" in stripped:
        if not default_currency:
            return value, None
        code = default_currency
        numeric_part = stripped.replace("$", "").strip()

    if code is None:
        return value, None

    numeric_canonical, _ = standardize_numeric(numeric_part, locale)
    canonical = f"{numeric_canonical} {code}"
    if canonical == value:
        return value, None
    return canonical, RULE_CURRENCY_FORMAT_NORMALIZE
