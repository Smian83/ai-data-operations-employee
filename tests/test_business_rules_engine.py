"""Tests for Module 21 pure resolver engine."""
import uuid
from unittest.mock import MagicMock

import pytest

from app.models.business_rule_global_default import BusinessRuleGlobalDefault
from app.models.business_rule_set import BusinessRuleSet
from app.models.business_rule_set_item import BusinessRuleSetItem
from app.rules.registry import BUILTIN_DEFAULTS, RULE_SCHEMA_VERSION
from app.rules.resolver import RESOLVER_VERSION, resolve_rules


def _make_global(rule_key, rule_value):
    gd = MagicMock(spec=BusinessRuleGlobalDefault)
    gd.rule_key = rule_key
    gd.rule_value = rule_value
    return gd


def _make_item(rule_key, rule_value):
    item = MagicMock(spec=BusinessRuleSetItem)
    item.rule_key = rule_key
    item.rule_value = rule_value
    return item


def _make_rule_set(version=1, rule_schema_version=RULE_SCHEMA_VERSION):
    rs = MagicMock(spec=BusinessRuleSet)
    rs.version = version
    rs.rule_schema_version = rule_schema_version
    rs.id = uuid.uuid4()
    return rs


ORG_ID = uuid.uuid4()
DS_ID = uuid.uuid4()


def _base_resolve(**kwargs):
    defaults = dict(
        organization_id=ORG_ID,
        data_source_id=DS_ID,
        global_defaults=[],
        ds_scoped_items=[],
        org_wide_items=[],
        ds_rule_set=None,
        org_rule_set=None,
    )
    defaults.update(kwargs)
    return resolve_rules(**defaults)


def test_ds_scoped_overrides_org_wide():
    ds_item = _make_item("detection.outlier_zscore_threshold", 2.0)
    org_item = _make_item("detection.outlier_zscore_threshold", 5.0)
    result = _base_resolve(ds_scoped_items=[ds_item], org_wide_items=[org_item])
    assert result.resolved_rules["detection.outlier_zscore_threshold"] == 2.0


def test_org_wide_overrides_global_default():
    global_d = _make_global("detection.outlier_zscore_threshold", 9.9)
    org_item = _make_item("detection.outlier_zscore_threshold", 5.0)
    result = _base_resolve(global_defaults=[global_d], org_wide_items=[org_item])
    assert result.resolved_rules["detection.outlier_zscore_threshold"] == 5.0


def test_global_default_overrides_builtin():
    global_d = _make_global("detection.outlier_zscore_threshold", 9.9)
    result = _base_resolve(global_defaults=[global_d])
    assert result.resolved_rules["detection.outlier_zscore_threshold"] == 9.9


def test_missing_key_falls_to_builtin():
    # No global defaults, no items — should fall through to BUILTIN_DEFAULTS
    result = _base_resolve()
    for key, value in BUILTIN_DEFAULTS.items():
        assert key in result.resolved_rules
        assert result.resolved_rules[key] == value


def test_two_orgs_isolation():
    org_a = uuid.uuid4()
    org_b = uuid.uuid4()
    item_a = _make_item("detection.outlier_zscore_threshold", 1.0)
    item_b = _make_item("detection.outlier_zscore_threshold", 9.0)

    result_a = resolve_rules(
        organization_id=org_a, data_source_id=None,
        global_defaults=[], ds_scoped_items=[], org_wide_items=[item_a],
        ds_rule_set=None, org_rule_set=None,
    )
    result_b = resolve_rules(
        organization_id=org_b, data_source_id=None,
        global_defaults=[], ds_scoped_items=[], org_wide_items=[item_b],
        ds_rule_set=None, org_rule_set=None,
    )
    # Org A's rules should not bleed into org B's
    assert result_a.resolved_rules["detection.outlier_zscore_threshold"] == 1.0
    assert result_b.resolved_rules["detection.outlier_zscore_threshold"] == 9.0
    assert result_a.organization_id == org_a
    assert result_b.organization_id == org_b


def test_sha256_is_deterministic():
    r1 = _base_resolve()
    r2 = _base_resolve()
    assert r1.resolved_rules_sha256 == r2.resolved_rules_sha256


def test_sha256_changes_with_different_values():
    item_v1 = _make_item("detection.outlier_zscore_threshold", 2.0)
    item_v2 = _make_item("detection.outlier_zscore_threshold", 5.0)
    r1 = _base_resolve(org_wide_items=[item_v1])
    r2 = _base_resolve(org_wide_items=[item_v2])
    assert r1.resolved_rules_sha256 != r2.resolved_rules_sha256


def test_repeatability_same_inputs_same_output():
    item = _make_item("detection.outlier_zscore_threshold", 3.7)
    global_d = _make_global("export.min_quality_score", 0.8)
    rs = _make_rule_set(version=2)

    r1 = resolve_rules(
        organization_id=ORG_ID, data_source_id=DS_ID,
        global_defaults=[global_d], ds_scoped_items=[],
        org_wide_items=[item], ds_rule_set=None, org_rule_set=rs,
    )
    r2 = resolve_rules(
        organization_id=ORG_ID, data_source_id=DS_ID,
        global_defaults=[global_d], ds_scoped_items=[],
        org_wide_items=[item], ds_rule_set=None, org_rule_set=rs,
    )
    assert r1 == r2


def test_rule_set_id_from_ds_scoped_when_both_present():
    ds_rs = _make_rule_set(version=3)
    org_rs = _make_rule_set(version=1)
    result = _base_resolve(ds_rule_set=ds_rs, org_rule_set=org_rs)
    assert result.rule_set_id == ds_rs.id
    assert result.rule_set_version == 3


def test_rule_set_id_from_org_when_no_ds():
    org_rs = _make_rule_set(version=2)
    result = _base_resolve(org_rule_set=org_rs)
    assert result.rule_set_id == org_rs.id
    assert result.rule_set_version == 2


def test_no_rule_set_gives_none():
    result = _base_resolve()
    assert result.rule_set_id is None
    assert result.rule_set_version is None


def test_resolver_version_constant():
    result = _base_resolve()
    assert result.resolver_version == RESOLVER_VERSION


def test_rule_schema_version_from_winning_set():
    rs = _make_rule_set(rule_schema_version="2.0")
    result = _base_resolve(org_rule_set=rs)
    assert result.rule_schema_version == "2.0"


def test_rule_schema_version_default_when_no_set():
    result = _base_resolve()
    assert result.rule_schema_version == RULE_SCHEMA_VERSION
