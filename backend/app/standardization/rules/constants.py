"""
Fixed, versioned rule-name/confidence/reason constants shared by every
rules/ submodule, mirroring app.cleaning.rules' RULE_CONFIDENCE/RULE_
REASONS pattern exactly. Confidence values are fixed per rule, never
computed from the data -- mechanical, unambiguous transforms (upper/lower
casing, boolean-form normalization) are highest; parsing-dependent or
policy-dependent transforms (date/time reparsing, phone E.164, currency)
are lower, same rationale Module 6 already established.

The universal whitespace-trim pass every classified column goes through
first (Section 6) reuses app.cleaning.rules.trim_whitespace directly
rather than a Module-7-local reimplementation -- its rule name and
RULE_CONFIDENCE/RULE_REASONS entry are imported and merged into this
module's own dicts below, so the engine's single _record() helper can
look up ANY rule name (Module 6's reused trim rule or one of Module 7's
own) against one consistent table, without a special-cased lookup path.
"""
from app.cleaning.rules import RULE_TRIM_WHITESPACE
from app.cleaning.rules import RULE_CONFIDENCE as _CLEANING_RULE_CONFIDENCE
from app.cleaning.rules import RULE_REASONS as _CLEANING_RULE_REASONS

RULE_TITLE_CASE = "title_case"
RULE_UPPER_CASE = "upper_case"
RULE_LOWER_CASE = "lower_case"
RULE_COMPANY_SUFFIX_LOOKUP = "company_suffix_lookup"
RULE_PHONE_E164 = "phone_e164"
RULE_ADDRESS_ABBREVIATION = "address_abbreviation_expansion"
RULE_STATE_PROVINCE_ABBREVIATION = "state_province_abbreviation"
RULE_COUNTRY_ISO_NORMALIZE = "country_iso_normalize"
RULE_POSTAL_CODE_FORMAT = "postal_code_format"
RULE_DATE_FORMAT_NORMALIZE = "date_format_normalize"
RULE_TIME_FORMAT_NORMALIZE = "time_format_normalize"
RULE_BOOLEAN_FORMAT_NORMALIZE = "boolean_format_normalize"
RULE_NUMERIC_FORMAT_NORMALIZE = "numeric_format_normalize"
RULE_CURRENCY_FORMAT_NORMALIZE = "currency_format_normalize"
RULE_ABBREVIATION_LOOKUP = "abbreviation_lookup"

RULE_CONFIDENCE: dict[str, float] = {
    RULE_TRIM_WHITESPACE: _CLEANING_RULE_CONFIDENCE[RULE_TRIM_WHITESPACE],
    RULE_TITLE_CASE: 0.95,
    RULE_UPPER_CASE: 1.0,
    RULE_LOWER_CASE: 1.0,
    RULE_COMPANY_SUFFIX_LOOKUP: 0.9,
    RULE_PHONE_E164: 0.85,
    RULE_ADDRESS_ABBREVIATION: 0.85,
    RULE_STATE_PROVINCE_ABBREVIATION: 0.9,
    RULE_COUNTRY_ISO_NORMALIZE: 0.9,
    RULE_POSTAL_CODE_FORMAT: 0.9,
    RULE_DATE_FORMAT_NORMALIZE: 0.7,
    RULE_TIME_FORMAT_NORMALIZE: 0.75,
    RULE_BOOLEAN_FORMAT_NORMALIZE: 0.95,
    RULE_NUMERIC_FORMAT_NORMALIZE: 0.85,
    RULE_CURRENCY_FORMAT_NORMALIZE: 0.8,
    RULE_ABBREVIATION_LOOKUP: 0.85,
}

RULE_REASONS: dict[str, str] = {
    RULE_TRIM_WHITESPACE: _CLEANING_RULE_REASONS[RULE_TRIM_WHITESPACE],
    RULE_TITLE_CASE: "Value converted to title case.",
    RULE_UPPER_CASE: "Value converted to upper case.",
    RULE_LOWER_CASE: "Value converted to lower case.",
    RULE_COMPANY_SUFFIX_LOOKUP: "Company suffix normalized via configured lookup table.",
    RULE_PHONE_E164: "Phone number reformatted to E.164 using the resolved country.",
    RULE_ADDRESS_ABBREVIATION: "Address term normalized via abbreviation lookup table.",
    RULE_STATE_PROVINCE_ABBREVIATION: "State/province normalized to its standard abbreviation.",
    RULE_COUNTRY_ISO_NORMALIZE: "Country name normalized to its ISO 3166-1 alpha-2 code.",
    RULE_POSTAL_CODE_FORMAT: "Postal/ZIP code reformatted to the resolved country's standard form.",
    RULE_DATE_FORMAT_NORMALIZE: "Date reparsed and reformatted to the canonical/configured form.",
    RULE_TIME_FORMAT_NORMALIZE: "Time reparsed and reformatted to 24-hour ISO-8601 form.",
    RULE_BOOLEAN_FORMAT_NORMALIZE: "Boolean value normalized to its canonical/configured form.",
    RULE_NUMERIC_FORMAT_NORMALIZE: "Numeric separators normalized to a canonical machine-readable form.",
    RULE_CURRENCY_FORMAT_NORMALIZE: "Currency value normalized to '<amount> <ISO4217code>' form.",
    RULE_ABBREVIATION_LOOKUP: "Value normalized via a configured generic abbreviation lookup table.",
}
