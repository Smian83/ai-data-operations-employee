"""
Built-in default static lookup tables (design doc Section 6). All keys
are lower-case for case-insensitive matching against already-trimmed
input. Organization-supplied StandardizationLookupEntry rows always take
precedence over these defaults for the same key (resolved by the caller
before these are ever consulted -- see app.standardization.engine).
"""

# Common English-language postal-address abbreviations: input variant
# (lower-case) -> canonical expanded form. Light-touch only, per the
# design's explicit "no aggressive rewriting" scope limit.
DEFAULT_ADDRESS_ABBREVIATIONS: dict[str, str] = {
    "st": "Street",
    "st.": "Street",
    "ave": "Avenue",
    "ave.": "Avenue",
    "blvd": "Boulevard",
    "blvd.": "Boulevard",
    "rd": "Road",
    "rd.": "Road",
    "dr": "Drive",
    "dr.": "Drive",
    "ln": "Lane",
    "ln.": "Lane",
    "ct": "Court",
    "ct.": "Court",
    "apt": "Apartment",
    "apt.": "Apartment",
    "ste": "Suite",
    "ste.": "Suite",
    "hwy": "Highway",
    "hwy.": "Highway",
}

# Common free-text country name variants -> ISO 3166-1 alpha-2. Not
# exhaustive by design -- unrecognized values are left untouched, never
# guessed (Section 6).
DEFAULT_COUNTRY_NAME_VARIANTS: dict[str, str] = {
    "us": "US",
    "usa": "US",
    "u.s.a.": "US",
    "u.s.": "US",
    "united states": "US",
    "united states of america": "US",
    "ca": "CA",
    "canada": "CA",
    "uk": "GB",
    "u.k.": "GB",
    "united kingdom": "GB",
    "great britain": "GB",
    "gb": "GB",
    "mx": "MX",
    "mexico": "MX",
    "au": "AU",
    "australia": "AU",
    "de": "DE",
    "germany": "DE",
    "fr": "FR",
    "france": "FR",
}

# US state name -> USPS two-letter abbreviation. Only applied when the
# row's resolved country is "US" (Section 6).
DEFAULT_US_STATE_ABBREVIATIONS: dict[str, str] = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "florida": "FL", "georgia": "GA", "hawaii": "HI", "idaho": "ID",
    "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI", "south carolina": "SC",
    "south dakota": "SD", "tennessee": "TN", "texas": "TX", "utah": "UT",
    "vermont": "VT", "virginia": "VA", "washington": "WA", "west virginia": "WV",
    "wisconsin": "WI", "wyoming": "WY", "district of columbia": "DC",
}

# Canadian province/territory name -> two-letter abbreviation. Only
# applied when the row's resolved country is "CA".
DEFAULT_CA_PROVINCE_ABBREVIATIONS: dict[str, str] = {
    "alberta": "AB", "british columbia": "BC", "manitoba": "MB",
    "new brunswick": "NB", "newfoundland and labrador": "NL",
    "northwest territories": "NT", "nova scotia": "NS", "nunavut": "NU",
    "ontario": "ON", "prince edward island": "PE", "quebec": "QC",
    "saskatchewan": "SK", "yukon": "YT",
}

# Unambiguous currency symbol -> ISO 4217 code. "$" is deliberately
# excluded here -- it is ambiguous across USD/CAD/AUD/etc. and is
# resolved via StandardizationConfig.default_currency instead, never a
# built-in guess (Section 6).
DEFAULT_CURRENCY_SYMBOL_MAP: dict[str, str] = {
    "€": "EUR",
    "£": "GBP",
    "¥": "JPY",
}
