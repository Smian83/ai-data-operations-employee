"""Module 7 unit tests: app.standardization.classification. Pure -- no DB,
no client. Verifies the built-in header-name heuristic for every field
type, case/separator-insensitivity, the "unmatched/ambiguous -> None,
never a guess" behavior, and organization-override precedence over the
heuristic."""
from app.standardization.classification import classify_column, classify_columns


def test_classifies_email_header_variants():
    for header in ("Email", "E-Mail", "e_mail", "EmailAddress", "  email  "):
        assert classify_column(header, {}) == "email"


def test_classifies_phone_header_variants():
    for header in ("Phone", "Phone Number", "Telephone", "Mobile", "Cell", "Fax"):
        assert classify_column(header, {}) == "phone"


def test_classifies_person_name_header_variants():
    for header in ("First Name", "LastName", "Full Name", "Contact Name"):
        assert classify_column(header, {}) == "person_name"


def test_classifies_company_name_header_variants():
    for header in ("Company", "Company Name", "Organization", "Business Name"):
        assert classify_column(header, {}) == "company_name"


def test_classifies_postal_address_header_variants():
    for header in ("Address", "Street Address", "Address Line"):
        assert classify_column(header, {}) == "postal_address"


def test_classifies_city_header_variants():
    for header in ("City", "Town"):
        assert classify_column(header, {}) == "city"


def test_classifies_state_province_header_variants():
    for header in ("State", "Province", "Region"):
        assert classify_column(header, {}) == "state_province"


def test_classifies_country_header_variants():
    for header in ("Country", "Nation"):
        assert classify_column(header, {}) == "country"


def test_classifies_postal_code_header_variants():
    for header in ("Zip", "ZipCode", "Postal Code", "postalcode"):
        assert classify_column(header, {}) == "postal_code"


def test_classifies_date_header_variants():
    for header in ("Date", "DOB", "Birth Date"):
        assert classify_column(header, {}) == "date"


def test_classifies_time_header():
    assert classify_column("Time", {}) == "time"


def test_classifies_boolean_header_variants():
    for header in ("Is Active", "Active", "Enabled", "Flag"):
        assert classify_column(header, {}) == "boolean"


def test_classifies_numeric_header_variants():
    for header in ("Amount", "Quantity", "Qty", "Count"):
        assert classify_column(header, {}) == "numeric"


def test_classifies_currency_header_variants():
    for header in ("Price", "Cost", "Salary", "Revenue"):
        assert classify_column(header, {}) == "currency"


def test_unmatched_header_classifies_as_unclassified_not_a_guess():
    assert classify_column("Random Column XYZ", {}) is None


def test_ambiguous_short_header_classifies_as_unclassified():
    """A column literally named 'code' is a real example from the design
    doc of an inherently ambiguous header (postal code? product code?
    something else?) -- must never be guessed."""
    assert classify_column("code", {}) is None


def test_blank_header_classifies_as_unclassified():
    assert classify_column("   ", {}) is None


def test_organization_override_takes_precedence_over_heuristic():
    """Even a header the heuristic would confidently classify one way is
    overridden by an explicit organization mapping."""
    overrides = {"random column xyz": "email"}
    assert classify_column("Random Column XYZ", overrides) == "email"


def test_organization_override_is_case_insensitive_exact_match():
    overrides = {"custom col": "phone"}
    assert classify_column("Custom Col", overrides) == "phone"
    assert classify_column("CUSTOM COL", overrides) == "phone"


def test_organization_override_does_not_affect_unrelated_headers():
    overrides = {"foo": "email"}
    assert classify_column("Bar", overrides) is None


def test_classify_columns_applies_classify_column_positionally():
    result = classify_columns(["Email", "Random Column XYZ", "Phone"], {})
    assert result == ["email", None, "phone"]
