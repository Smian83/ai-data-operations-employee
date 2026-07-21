"""
Email address / phone number standardization (design doc Section 6).
Email: conservative syntactic check + lower-case only -- malformed
values are left untouched, never rewritten or guessed. Phone: E.164 via
the `phonenumbers` library (deterministic offline metadata, not AI/ML,
no network call -- see the design doc's explicit justification), only
attempted when a country can be confidently resolved for that row.
"""
from __future__ import annotations

import phonenumbers

from app.standardization.rules.casing import lower_case
from app.standardization.rules.constants import RULE_LOWER_CASE, RULE_PHONE_E164


def standardize_email(value: str) -> tuple[str, str | None]:
    if value == "":
        return value, None
    parts = value.split("@")
    if len(parts) != 2 or not parts[0] or not parts[1]:
        # Malformed (not exactly one '@', or an empty local/domain part) --
        # left untouched, never rewritten or guessed.
        return value, None

    lowered = lower_case(value)
    if lowered == value:
        return value, None
    return lowered, RULE_LOWER_CASE


def standardize_phone(value: str, country: str | None) -> tuple[str, str | None]:
    """country is the already-resolved ISO 3166-1 alpha-2 code for this
    row (from a standardized `country` column or
    StandardizationConfig.default_country), or None if unresolved -- in
    which case E.164 conversion is never attempted, per the design's
    explicit "without a resolvable country, left unchanged" rule."""
    if value == "" or not country:
        return value, None
    try:
        parsed = phonenumbers.parse(value, country)
    except phonenumbers.NumberParseException:
        return value, None
    if not phonenumbers.is_valid_number(parsed):
        return value, None

    formatted = phonenumbers.format_number(parsed, phonenumbers.PhoneNumberFormat.E164)
    if formatted == value:
        return value, None
    return formatted, RULE_PHONE_E164
