"""
Module 7: orchestrates the field-type-dispatched standardization pipeline
described in docs/module-7-data-standardization-engine-design.md Sections
1, 6. `standardize()` is pure -- no I/O, no randomness, no AI/ML -- so it
always produces an identical StandardizationResult for identical input,
rule set, and organization configuration, which is this engine's
determinism acceptance criterion. It is also self-idempotent: feeding a
StandardizationResult's own standardized_rows back into standardize()
under the same classification/config must yield zero changes, since
every rule function is a no-op once its target value is already in
canonical form (see each rules/ submodule and the idempotency tests).
StandardizationHandler (app.worker.handlers.standardization) is the only
caller; all file I/O and persistence happen there, not here.
"""
from __future__ import annotations

from app.cleaning.rules import trim_whitespace
from app.standardization.rules.abbreviation import apply_generic_abbreviation_lookup
from app.standardization.rules.casing import title_case_step
from app.standardization.rules.constants import RULE_CONFIDENCE, RULE_REASONS
from app.standardization.rules.contact import standardize_email, standardize_phone
from app.standardization.rules.geo import (
    expand_address_abbreviations,
    resolve_country_code,
    standardize_city,
    standardize_country,
    standardize_postal_code,
    standardize_state_province,
)
from app.standardization.rules.lookups import DEFAULT_ADDRESS_ABBREVIATIONS
from app.standardization.rules.names import standardize_company_name, standardize_person_name
from app.standardization.rules.temporal import standardize_date, standardize_time
from app.standardization.rules.values import (
    standardize_boolean,
    standardize_currency,
    standardize_numeric,
)
from app.standardization.types import Change, LookupTables, StandardizationConfig, StandardizationLimits, StandardizationResult

# Bumped whenever any rule's OUTPUT could change for existing input.
# Recorded on every StandardizationRun (standardization_engine_version)
# AND on every individual StandardizationChange (rule_version) -- see
# docs/module-7-data-standardization-engine-design.md Sections 3, 7.
STANDARDIZATION_ENGINE_VERSION = "1.0"

# "Free text" field types the generic (field_type=NULL) abbreviation pass
# applies to, as a final step after the field's own type-specific rule(s)
# -- numeric/date/time/boolean/currency/phone/email/postal_code/postal_
# address have their own strict canonical forms that a generic lookup
# pass could corrupt, so it is deliberately scoped to free-text fields
# only. postal_address already has its own address-specific abbreviation
# pass (Section 6) and is excluded here to avoid a confusing double
# lookup against two different tables for the same cell.
_GENERIC_ABBREVIATION_FIELD_TYPES = {
    "person_name", "company_name", "city", "state_province", "country",
}


def standardize(
    rows: list[list[str]],
    headers: list[str],
    field_types: list[str | None],
    lookup_tables: LookupTables,
    config: StandardizationConfig,
    limits: StandardizationLimits,
) -> StandardizationResult:
    """`field_types[i]` is the already-resolved classification (org
    override, else header heuristic, else None) for `headers[i]` --
    classification itself is app.standardization.classification's job,
    called by StandardizationHandler before this function, not here.
    `rows` must already be uniform-length (guaranteed by
    app.profiling.csv_loader.load_csv, reused unchanged, same as Module
    6)."""
    country_column_index = next(
        (i for i, ft in enumerate(field_types) if ft == "country"), None
    )
    country_lookup = lookup_tables.for_field_type("country")
    address_lookup = {**DEFAULT_ADDRESS_ABBREVIATIONS, **lookup_tables.for_field_type("postal_address")}
    company_lookup = lookup_tables.for_field_type("company_name")

    standardized_rows: list[list[str]] = []
    changes: list[Change] = []
    changes_by_rule: dict[str, int] = {}

    for row_index, row in enumerate(rows):
        standardized_row = list(row)

        # Country resolved FIRST, since phone/state_province/postal_code
        # rules for every other column on this row depend on it.
        row_country: str | None = config.default_country
        if country_column_index is not None:
            raw_country_value = row[country_column_index]
            trimmed, trim_rule = trim_whitespace(raw_country_value)
            _record(
                changes, changes_by_rule, row_index,
                headers[country_column_index], "country",
                raw_country_value, trimmed, trim_rule,
            )
            new_value, rule_name = standardize_country(trimmed, country_lookup)
            _record(
                changes, changes_by_rule, row_index,
                headers[country_column_index], "country",
                trimmed, new_value, rule_name,
            )
            standardized_row[country_column_index] = new_value
            resolved = resolve_country_code(new_value, country_lookup)
            if resolved is not None:
                row_country = resolved

        for column_index, raw_value in enumerate(row):
            if column_index == country_column_index:
                continue  # already handled above
            field_type = field_types[column_index]
            if field_type is None:
                continue  # unclassified -- left alone, never guessed

            column_name = headers[column_index]
            value, trim_rule = trim_whitespace(raw_value)
            _record(changes, changes_by_rule, row_index, column_name, field_type, raw_value, value, trim_rule)
            before_field_rule = value

            if field_type == "person_name":
                value, rule_name = standardize_person_name(value)
            elif field_type == "company_name":
                value, rule_name = standardize_company_name(value, company_lookup)
            elif field_type == "email":
                value, rule_name = standardize_email(value)
            elif field_type == "phone":
                value, rule_name = standardize_phone(value, row_country)
            elif field_type == "postal_address":
                before_abbrev = value
                value, rule_name = expand_address_abbreviations(value, address_lookup)
                _record(changes, changes_by_rule, row_index, column_name, field_type, before_abbrev, value, rule_name)
                before_casing = value
                value, rule_name = title_case_step(value)
                _record(changes, changes_by_rule, row_index, column_name, field_type, before_casing, value, rule_name)
                standardized_row[column_index] = value
                continue
            elif field_type == "city":
                value, rule_name = standardize_city(value)
            elif field_type == "state_province":
                value, rule_name = standardize_state_province(value, row_country)
            elif field_type == "postal_code":
                value, rule_name = standardize_postal_code(value, row_country)
            elif field_type == "date":
                value, rule_name = standardize_date(value, config.date_output_format)
            elif field_type == "time":
                value, rule_name = standardize_time(value)
            elif field_type == "boolean":
                value, rule_name = standardize_boolean(value, config.boolean_output_form)
            elif field_type == "numeric":
                value, rule_name = standardize_numeric(value, config.numeric_locale)
            elif field_type == "currency":
                value, rule_name = standardize_currency(
                    value, config.default_currency, config.numeric_locale
                )
            else:  # pragma: no cover -- defensive; field_type is validated upstream
                rule_name = None

            _record(changes, changes_by_rule, row_index, column_name, field_type, before_field_rule, value, rule_name)

            if field_type in _GENERIC_ABBREVIATION_FIELD_TYPES:
                before_generic = value
                value, rule_name = apply_generic_abbreviation_lookup(value, lookup_tables.global_)
                _record(changes, changes_by_rule, row_index, column_name, field_type, before_generic, value, rule_name)

            standardized_row[column_index] = value

        standardized_rows.append(standardized_row)

    total_changes_count = len(changes)
    persisted_changes = changes[: limits.max_persisted_changes]
    confidence_score = min((change.confidence for change in changes), default=1.0)

    return StandardizationResult(
        standardized_rows=standardized_rows,
        changes=persisted_changes,
        total_changes_count=total_changes_count,
        changes_by_rule=changes_by_rule,
        confidence_score=confidence_score,
    )


def _record(
    changes: list[Change],
    changes_by_rule: dict[str, int],
    row_index: int,
    column_name: str,
    field_type: str,
    before: str,
    after: str,
    rule_name: str | None,
) -> None:
    if rule_name is None or before == after:
        return
    changes.append(
        Change(
            row_index=row_index,
            column_name=column_name,
            field_type=field_type,
            original_value=before,
            standardized_value=after,
            rule_name=rule_name,
            rule_version=STANDARDIZATION_ENGINE_VERSION,
            reason=RULE_REASONS[rule_name],
            confidence=RULE_CONFIDENCE[rule_name],
        )
    )
    changes_by_rule[rule_name] = changes_by_rule.get(rule_name, 0) + 1
