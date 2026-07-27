"""Module 21 — Business Rules API.

All endpoints are org-scoped via JWT. organization_id is inferred from the
authenticated user's token — it is NOT in the URL path.

Endpoints:
  GET    /api/v1/business-rules/global-defaults
  GET    /api/v1/business-rules/rule-sets
  POST   /api/v1/business-rules/rule-sets
  GET    /api/v1/business-rules/rule-sets/{rule_set_id}
  POST   /api/v1/business-rules/rule-sets/{rule_set_id}/publish
  POST   /api/v1/business-rules/rule-sets/{rule_set_id}/activate
  POST   /api/v1/business-rules/rule-sets/{rule_set_id}/items
  PUT    /api/v1/business-rules/rule-sets/{rule_set_id}/items/{item_id}
  DELETE /api/v1/business-rules/rule-sets/{rule_set_id}/items/{item_id}
  GET    /api/v1/business-rules/runs
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_active_user, get_db
from app.models.business_rule_global_default import BusinessRuleGlobalDefault
from app.models.business_rule_set import BusinessRuleSet
from app.models.business_rule_set_item import BusinessRuleSetItem
from app.models.business_rule_set_run import BusinessRuleSetRun
from app.models.enums import BUSINESS_RULE_TYPES, BUSINESS_RULE_PIPELINE_RUN_TYPES
from app.models.user import User
from app.schemas.business_rules import (
    GlobalDefaultOut,
    RuleSetCreate,
    RuleSetDetailOut,
    RuleSetItemCreate,
    RuleSetItemOut,
    RuleSetItemUpdate,
    RuleSetOut,
    RuleSetRunOut,
)
from app.rules.registry import RULE_SCHEMA_VERSION

router = APIRouter(prefix="/api/v1/business-rules", tags=["business-rules"])


# ---------------------------------------------------------------------------
# Global defaults (read-only)
# ---------------------------------------------------------------------------

@router.get("/global-defaults", response_model=list[GlobalDefaultOut])
def list_global_defaults(
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> list[BusinessRuleGlobalDefault]:
    rows = (
        db.execute(
            select(BusinessRuleGlobalDefault).order_by(
                BusinessRuleGlobalDefault.rule_key
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


# ---------------------------------------------------------------------------
# Rule sets
# ---------------------------------------------------------------------------

@router.get("/rule-sets", response_model=list[RuleSetOut])
def list_rule_sets(
    is_draft: Optional[bool] = Query(None),
    is_active: Optional[bool] = Query(None),
    data_source_id: Optional[uuid.UUID] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> list[BusinessRuleSet]:
    q = select(BusinessRuleSet).where(
        BusinessRuleSet.organization_id == current_user.organization_id
    )
    if is_draft is not None:
        q = q.where(BusinessRuleSet.is_draft.is_(is_draft))
    if is_active is not None:
        q = q.where(BusinessRuleSet.is_active.is_(is_active))
    if data_source_id is not None:
        q = q.where(BusinessRuleSet.data_source_id == data_source_id)
    q = q.order_by(BusinessRuleSet.created_at.desc())
    return list(db.execute(q).scalars().all())


@router.post("/rule-sets", response_model=RuleSetOut, status_code=status.HTTP_201_CREATED)
def create_rule_set(
    body: RuleSetCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> BusinessRuleSet:
    rule_set = BusinessRuleSet(
        id=uuid.uuid4(),
        organization_id=current_user.organization_id,
        data_source_id=body.data_source_id,
        version=1,
        name=body.name,
        description=body.description,
        is_draft=True,
        is_active=False,
        rule_schema_version=RULE_SCHEMA_VERSION,
        created_by_identity=current_user.email,
    )
    db.add(rule_set)
    db.commit()
    db.refresh(rule_set)
    return rule_set


@router.get("/rule-sets/{rule_set_id}", response_model=RuleSetDetailOut)
def get_rule_set(
    rule_set_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> BusinessRuleSet:
    rule_set = db.execute(
        select(BusinessRuleSet).where(
            BusinessRuleSet.id == rule_set_id,
            BusinessRuleSet.organization_id == current_user.organization_id,
        )
    ).scalar_one_or_none()
    if rule_set is None:
        raise HTTPException(status_code=404, detail="Rule set not found")
    return rule_set


@router.post("/rule-sets/{rule_set_id}/publish", response_model=RuleSetOut)
def publish_rule_set(
    rule_set_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> BusinessRuleSet:
    rule_set = db.execute(
        select(BusinessRuleSet).where(
            BusinessRuleSet.id == rule_set_id,
            BusinessRuleSet.organization_id == current_user.organization_id,
        )
    ).scalar_one_or_none()
    if rule_set is None:
        raise HTTPException(status_code=404, detail="Rule set not found")
    if not rule_set.is_draft:
        raise HTTPException(status_code=409, detail="Rule set is already published")

    # Compute next version number for this org+scope
    existing_versions = db.execute(
        select(BusinessRuleSet.version).where(
            BusinessRuleSet.organization_id == current_user.organization_id,
            BusinessRuleSet.data_source_id == rule_set.data_source_id,
            BusinessRuleSet.is_draft.is_(False),
        )
    ).scalars().all()
    next_version = max(existing_versions, default=0) + 1

    rule_set.is_draft = False
    rule_set.version = next_version
    rule_set.published_at = datetime.now(timezone.utc)
    rule_set.published_by_identity = current_user.email
    db.commit()
    db.refresh(rule_set)
    return rule_set


@router.post("/rule-sets/{rule_set_id}/activate", response_model=RuleSetOut)
def activate_rule_set(
    rule_set_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> BusinessRuleSet:
    rule_set = db.execute(
        select(BusinessRuleSet).where(
            BusinessRuleSet.id == rule_set_id,
            BusinessRuleSet.organization_id == current_user.organization_id,
        )
    ).scalar_one_or_none()
    if rule_set is None:
        raise HTTPException(status_code=404, detail="Rule set not found")
    if rule_set.is_draft:
        raise HTTPException(status_code=409, detail="Cannot activate a draft rule set; publish it first")

    # Deactivate any currently active set in the same scope
    existing_active = db.execute(
        select(BusinessRuleSet).where(
            BusinessRuleSet.organization_id == current_user.organization_id,
            BusinessRuleSet.data_source_id == rule_set.data_source_id,
            BusinessRuleSet.is_active.is_(True),
            BusinessRuleSet.id != rule_set_id,
        )
    ).scalar_one_or_none()
    if existing_active is not None:
        existing_active.is_active = False
        # Flush the deactivation first so the partial unique index
        # (one active per scope) does not fire when the new row is marked
        # active in the same batch — SQLite enforces constraints per statement.
        db.flush()

    rule_set.is_active = True
    db.commit()
    db.refresh(rule_set)
    return rule_set


# ---------------------------------------------------------------------------
# Rule set items
# ---------------------------------------------------------------------------

def _get_draft_rule_set(
    db: Session,
    rule_set_id: uuid.UUID,
    organization_id: uuid.UUID,
) -> BusinessRuleSet:
    rule_set = db.execute(
        select(BusinessRuleSet).where(
            BusinessRuleSet.id == rule_set_id,
            BusinessRuleSet.organization_id == organization_id,
        )
    ).scalar_one_or_none()
    if rule_set is None:
        raise HTTPException(status_code=404, detail="Rule set not found")
    if not rule_set.is_draft:
        raise HTTPException(
            status_code=409,
            detail="Cannot modify a published rule set; create a new draft",
        )
    return rule_set


@router.post(
    "/rule-sets/{rule_set_id}/items",
    response_model=RuleSetItemOut,
    status_code=status.HTTP_201_CREATED,
)
def add_rule_set_item(
    rule_set_id: uuid.UUID,
    body: RuleSetItemCreate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> BusinessRuleSetItem:
    rule_set = _get_draft_rule_set(db, rule_set_id, current_user.organization_id)

    if body.rule_type not in BUSINESS_RULE_TYPES:
        raise HTTPException(status_code=422, detail=f"Invalid rule_type: {body.rule_type}")

    item = BusinessRuleSetItem(
        id=uuid.uuid4(),
        organization_id=current_user.organization_id,
        rule_set_id=rule_set.id,
        rule_key=body.rule_key,
        rule_type=body.rule_type,
        rule_source="organization",
        rule_value=body.rule_value,
    )
    db.add(item)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"A rule with key '{body.rule_key}' already exists in this rule set",
        )
    db.refresh(item)
    return item


@router.put(
    "/rule-sets/{rule_set_id}/items/{item_id}",
    response_model=RuleSetItemOut,
)
def update_rule_set_item(
    rule_set_id: uuid.UUID,
    item_id: uuid.UUID,
    body: RuleSetItemUpdate,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> BusinessRuleSetItem:
    _get_draft_rule_set(db, rule_set_id, current_user.organization_id)

    item = db.execute(
        select(BusinessRuleSetItem).where(
            BusinessRuleSetItem.id == item_id,
            BusinessRuleSetItem.rule_set_id == rule_set_id,
            BusinessRuleSetItem.organization_id == current_user.organization_id,
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Rule set item not found")

    item.rule_value = body.rule_value
    db.commit()
    db.refresh(item)
    return item


@router.delete(
    "/rule-sets/{rule_set_id}/items/{item_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    response_model=None,
)
def delete_rule_set_item(
    rule_set_id: uuid.UUID,
    item_id: uuid.UUID,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> None:
    _get_draft_rule_set(db, rule_set_id, current_user.organization_id)

    item = db.execute(
        select(BusinessRuleSetItem).where(
            BusinessRuleSetItem.id == item_id,
            BusinessRuleSetItem.rule_set_id == rule_set_id,
            BusinessRuleSetItem.organization_id == current_user.organization_id,
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(status_code=404, detail="Rule set item not found")

    db.delete(item)
    db.commit()


# ---------------------------------------------------------------------------
# Rule set audit runs
# ---------------------------------------------------------------------------

@router.get("/runs", response_model=list[RuleSetRunOut])
def list_runs(
    pipeline_run_type: Optional[str] = Query(None),
    limit: int = Query(50, ge=1, le=500),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> list[BusinessRuleSetRun]:
    q = select(BusinessRuleSetRun).where(
        BusinessRuleSetRun.organization_id == current_user.organization_id
    )
    if pipeline_run_type is not None:
        if pipeline_run_type not in BUSINESS_RULE_PIPELINE_RUN_TYPES:
            raise HTTPException(
                status_code=422,
                detail=f"Invalid pipeline_run_type: {pipeline_run_type}",
            )
        q = q.where(BusinessRuleSetRun.pipeline_run_type == pipeline_run_type)
    q = q.order_by(BusinessRuleSetRun.created_at.desc()).limit(limit)
    return list(db.execute(q).scalars().all())
