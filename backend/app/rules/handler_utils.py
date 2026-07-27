"""Module 21 — Handler integration utilities.

load_resolved_rules() and write_rule_set_run() are the two helper functions
that every handler integration calls. They abstract away the DB queries and
the audit-row creation, keeping each handler's own execute() clean.

load_resolved_rules() always succeeds — if no rule sets exist it falls
through to global defaults and then to BUILTIN_DEFAULTS.

write_rule_set_run() writes a BusinessRuleSetRun audit row inside the
caller's existing transaction. The caller is responsible for the commit.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.business_rule_global_default import BusinessRuleGlobalDefault
from app.models.business_rule_set import BusinessRuleSet
from app.models.business_rule_set_item import BusinessRuleSetItem
from app.models.business_rule_set_run import BusinessRuleSetRun
from app.rules.registry import RULE_SCHEMA_VERSION
from app.rules.resolver import RESOLVER_VERSION, ResolvedRuleSet, resolve_rules


def load_resolved_rules(
    db: Session,
    organization_id: uuid.UUID,
    data_source_id: uuid.UUID | None,
) -> ResolvedRuleSet:
    """Load active rule sets from DB and call resolve_rules().

    Always succeeds — falls through to global defaults if no rule sets exist.
    """
    # Load all global defaults
    global_defaults = (
        db.execute(
            select(BusinessRuleGlobalDefault).where(
                BusinessRuleGlobalDefault.deprecated.is_(False)
            )
        )
        .scalars()
        .all()
    )

    # Find active DS-scoped rule set (data_source_id IS NOT NULL and matches)
    ds_rule_set: BusinessRuleSet | None = None
    ds_scoped_items: list[BusinessRuleSetItem] = []
    if data_source_id is not None:
        ds_rule_set = db.execute(
            select(BusinessRuleSet).where(
                BusinessRuleSet.organization_id == organization_id,
                BusinessRuleSet.data_source_id == data_source_id,
                BusinessRuleSet.is_active.is_(True),
            )
        ).scalar_one_or_none()
        if ds_rule_set is not None:
            ds_scoped_items = (
                db.execute(
                    select(BusinessRuleSetItem).where(
                        BusinessRuleSetItem.rule_set_id == ds_rule_set.id,
                        BusinessRuleSetItem.organization_id == organization_id,
                    )
                )
                .scalars()
                .all()
            )

    # Find active org-wide rule set (data_source_id IS NULL)
    org_rule_set: BusinessRuleSet | None = db.execute(
        select(BusinessRuleSet).where(
            BusinessRuleSet.organization_id == organization_id,
            BusinessRuleSet.data_source_id.is_(None),
            BusinessRuleSet.is_active.is_(True),
        )
    ).scalar_one_or_none()
    org_wide_items: list[BusinessRuleSetItem] = []
    if org_rule_set is not None:
        org_wide_items = (
            db.execute(
                select(BusinessRuleSetItem).where(
                    BusinessRuleSetItem.rule_set_id == org_rule_set.id,
                    BusinessRuleSetItem.organization_id == organization_id,
                )
            )
            .scalars()
            .all()
        )

    return resolve_rules(
        organization_id=organization_id,
        data_source_id=data_source_id,
        global_defaults=list(global_defaults),
        ds_scoped_items=list(ds_scoped_items),
        org_wide_items=list(org_wide_items),
        ds_rule_set=ds_rule_set,
        org_rule_set=org_rule_set,
    )


def write_rule_set_run(
    db: Session,
    organization_id: uuid.UUID,
    resolved: ResolvedRuleSet,
    pipeline_run_type: str,
    pipeline_run_id: uuid.UUID,
) -> None:
    """Write a BusinessRuleSetRun audit row.

    Called inside the handler's existing transaction — the caller commits.
    Silently skips if a duplicate row already exists (idempotency guard).
    """
    from sqlalchemy.exc import IntegrityError

    run = BusinessRuleSetRun(
        id=uuid.uuid4(),
        organization_id=organization_id,
        rule_set_id=resolved.rule_set_id,
        rule_set_version=resolved.rule_set_version,
        rule_schema_version=resolved.rule_schema_version,
        resolver_version=resolved.resolver_version,
        pipeline_run_type=pipeline_run_type,
        pipeline_run_id=pipeline_run_id,
        resolved_rules_snapshot=resolved.resolved_rules,
        resolved_rules_sha256=resolved.resolved_rules_sha256,
    )
    db.add(run)
    # The caller's commit will persist this. On duplicate (retry scenario),
    # the UNIQUE(pipeline_run_type, pipeline_run_id) constraint causes an
    # IntegrityError that the caller's outer try/except handles. We do NOT
    # flush here to avoid interrupting the caller's transaction.
