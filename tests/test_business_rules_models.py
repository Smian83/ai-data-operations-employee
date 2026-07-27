"""Tests for Module 21 Business Rules DB models and constraints."""
import uuid

import pytest
from sqlalchemy.exc import IntegrityError

from app.models.business_rule_global_default import BusinessRuleGlobalDefault
from app.models.business_rule_set import BusinessRuleSet
from app.models.business_rule_set_item import BusinessRuleSetItem
from app.models.business_rule_set_run import BusinessRuleSetRun
from app.models.organization import Organization


def _make_org(db_session):
    org = Organization(id=uuid.uuid4(), name="BRTest Org", slug=f"brtest-{uuid.uuid4().hex[:8]}")
    db_session.add(org)
    db_session.commit()
    return org


def _make_rule_set(db_session, org_id, name="RS1", is_draft=True, is_active=False, version=1):
    rs = BusinessRuleSet(
        id=uuid.uuid4(),
        organization_id=org_id,
        version=version,
        name=name,
        is_draft=is_draft,
        is_active=is_active,
    )
    db_session.add(rs)
    db_session.commit()
    return rs


def test_global_default_valid_rule_type(db_session):
    gd = BusinessRuleGlobalDefault(
        id=uuid.uuid4(),
        rule_key=f"test.key.{uuid.uuid4().hex[:6]}",
        rule_type="threshold",
        rule_source="builtin",
        rule_value=3.5,
        introduced_version="21.0",
        deprecated=False,
    )
    db_session.add(gd)
    db_session.commit()
    assert gd.rule_type == "threshold"


def test_global_default_invalid_rule_type_rejected(db_session):
    gd = BusinessRuleGlobalDefault(
        id=uuid.uuid4(),
        rule_key=f"test.key.{uuid.uuid4().hex[:6]}",
        rule_type="invalid_type",
        rule_source="builtin",
        rule_value=1.0,
        introduced_version="21.0",
        deprecated=False,
    )
    db_session.add(gd)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_global_default_unique_rule_key(db_session):
    key = f"test.unique.{uuid.uuid4().hex[:6]}"
    gd1 = BusinessRuleGlobalDefault(
        id=uuid.uuid4(), rule_key=key, rule_type="threshold",
        rule_source="builtin", rule_value=1.0, introduced_version="21.0", deprecated=False,
    )
    gd2 = BusinessRuleGlobalDefault(
        id=uuid.uuid4(), rule_key=key, rule_type="toggle",
        rule_source="builtin", rule_value=False, introduced_version="21.0", deprecated=False,
    )
    db_session.add(gd1)
    db_session.commit()
    db_session.add(gd2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_rule_set_version_min_1(db_session):
    org = _make_org(db_session)
    rs = BusinessRuleSet(
        id=uuid.uuid4(), organization_id=org.id, version=0,
        name="Invalid", is_draft=True, is_active=False,
    )
    db_session.add(rs)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_rule_set_version_1_valid(db_session):
    org = _make_org(db_session)
    rs = _make_rule_set(db_session, org.id, version=1)
    assert rs.version == 1


def test_rule_set_item_unique_key(db_session):
    org = _make_org(db_session)
    rs = _make_rule_set(db_session, org.id)
    item1 = BusinessRuleSetItem(
        id=uuid.uuid4(), organization_id=org.id, rule_set_id=rs.id,
        rule_key="detection.outlier_zscore_threshold", rule_type="threshold",
        rule_source="organization", rule_value=4.0,
    )
    item2 = BusinessRuleSetItem(
        id=uuid.uuid4(), organization_id=org.id, rule_set_id=rs.id,
        rule_key="detection.outlier_zscore_threshold",  # duplicate
        rule_type="threshold", rule_source="organization", rule_value=5.0,
    )
    db_session.add(item1)
    db_session.commit()
    db_session.add(item2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_rule_set_item_different_keys_allowed(db_session):
    org = _make_org(db_session)
    rs = _make_rule_set(db_session, org.id)
    item1 = BusinessRuleSetItem(
        id=uuid.uuid4(), organization_id=org.id, rule_set_id=rs.id,
        rule_key="detection.outlier_zscore_threshold", rule_type="threshold",
        rule_source="organization", rule_value=4.0,
    )
    item2 = BusinessRuleSetItem(
        id=uuid.uuid4(), organization_id=org.id, rule_set_id=rs.id,
        rule_key="export.blocked_columns", rule_type="list",
        rule_source="organization", rule_value=["ssn"],
    )
    db_session.add_all([item1, item2])
    db_session.commit()
    assert item1.rule_set_id == item2.rule_set_id


def test_rule_set_run_unique_pipeline_run(db_session):
    org = _make_org(db_session)
    pipeline_run_id = uuid.uuid4()
    run1 = BusinessRuleSetRun(
        id=uuid.uuid4(), organization_id=org.id, resolver_version="1.0.0",
        pipeline_run_type="issue_detection", pipeline_run_id=pipeline_run_id,
        resolved_rules_snapshot={"k": "v"}, resolved_rules_sha256="abc123",
    )
    run2 = BusinessRuleSetRun(
        id=uuid.uuid4(), organization_id=org.id, resolver_version="1.0.0",
        pipeline_run_type="issue_detection", pipeline_run_id=pipeline_run_id,
        resolved_rules_snapshot={"k": "v2"}, resolved_rules_sha256="abc456",
    )
    db_session.add(run1)
    db_session.commit()
    db_session.add(run2)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()


def test_rule_set_run_different_types_allowed(db_session):
    org = _make_org(db_session)
    pipeline_run_id = uuid.uuid4()
    run1 = BusinessRuleSetRun(
        id=uuid.uuid4(), organization_id=org.id, resolver_version="1.0.0",
        pipeline_run_type="issue_detection", pipeline_run_id=pipeline_run_id,
        resolved_rules_snapshot={}, resolved_rules_sha256="aaa",
    )
    run2 = BusinessRuleSetRun(
        id=uuid.uuid4(), organization_id=org.id, resolver_version="1.0.0",
        pipeline_run_type="report", pipeline_run_id=pipeline_run_id,
        resolved_rules_snapshot={}, resolved_rules_sha256="bbb",
    )
    db_session.add_all([run1, run2])
    db_session.commit()


def test_org_isolation_item_cannot_reference_other_org_rule_set(db_session):
    org_a = _make_org(db_session)
    org_b = _make_org(db_session)
    rs_a = _make_rule_set(db_session, org_a.id)
    item = BusinessRuleSetItem(
        id=uuid.uuid4(), organization_id=org_b.id, rule_set_id=rs_a.id,
        rule_key="detection.outlier_zscore_threshold", rule_type="threshold",
        rule_source="organization", rule_value=4.0,
    )
    db_session.add(item)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
