"""
Module 7: column -> field_type classification (design doc Section 6).
Pure, deterministic, no I/O -- callers (the worker handler) resolve
organization overrides from the database first and pass the result in
as a plain dict; this module never touches the database itself.

Precedence: an explicit organization override (column_overrides) always
wins; otherwise the built-in header-name heuristic is tried; otherwise
the column is unclassified (None) and receives zero standardization
changes. An unmatched or ambiguous header is a safe, valid outcome by
design -- never a guess.
"""
from __future__ import annotations

import re

from app.models.enums import STANDARDIZATION_FIELD_TYPES

_WHITESPACE_RE = re.compile(r"\s+")

# Fixed, versioned dictionary of column-header patterns, checked
# case-insensitively against the header after whitespace trim/collapse.
# Order matters only in that the FIRST matching field_type wins if a
# header could plausibly match more than one pattern list -- lists are
# kept mutually exclusive in practice to avoid that ambiguity.
_HEADER_PATTERNS: dict[str, tuple[str, ...]] = {
    "email": ("email", "e-mail", "e_mail", "emailaddress"),
    "phone": ("phone", "telephone", "tel", "mobile", "cell", "fax"),
    "person_name": ("firstname", "first_name", "lastname", "last_name", "fullname", "full_name", "contactname", "contact_name"),
    "company_name": ("company", "companyname", "company_name", "organization", "org_name", "employer", "business_name"),
    "postal_address": ("address", "street", "addressline", "address_line", "street_address"),
    "city": ("city", "town"),
    "state_province": ("state", "province", "state_province", "region"),
    "country": ("country", "nation"),
    "postal_code": ("zip", "zipcode", "zip_code", "postal", "postalcode", "postal_code"),
    "date": ("date", "dob", "birthdate", "birth_date"),
    "time": ("time",),
    "boolean": ("isactive", "is_active", "active", "enabled", "flag"),
    "numeric": ("amount", "quantity", "qty", "count", "number", "num"),
    "currency": ("price", "cost", "salary", "revenue", "currency"),
}

assert set(_HEADER_PATTERNS.keys()).issubset(set(STANDARDIZATION_FIELD_TYPES)), (
    "classification._HEADER_PATTERNS references a field_type not in "
    "STANDARDIZATION_FIELD_TYPES"
)


def _normalize_header(header: str) -> str:
    collapsed = _WHITESPACE_RE.sub("", header.strip().lower())
    return collapsed.replace("-", "").replace("_", "")


def classify_column(header: str, column_overrides: dict[str, str]) -> str | None:
    """column_overrides is keyed by the SAME lower/trim normalization
    normalize_name+lower would produce (case-insensitive exact column-name
    match) -- callers are responsible for building it that way from
    StandardizationColumnMapping rows. Returns a field_type from
    STANDARDIZATION_FIELD_TYPES, or None if unclassified."""
    override_key = header.strip().lower()
    if override_key in column_overrides:
        return column_overrides[override_key]

    normalized = _normalize_header(header)
    if not normalized:
        return None
    for field_type, patterns in _HEADER_PATTERNS.items():
        for pattern in patterns:
            normalized_pattern = pattern.replace("-", "").replace("_", "")
            if normalized_pattern in normalized:
                return field_type
    return None


def classify_columns(headers: list[str], column_overrides: dict[str, str]) -> list[str | None]:
    return [classify_column(header, column_overrides) for header in headers]
