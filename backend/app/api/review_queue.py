"""Module 11 -- Operational Review Queue API.

A single, read-only endpoint (GET /review-queue). No claim, assignment,
locking, notification, escalation-timer, or workflow-engine behavior
exists here -- see docs/module-11-operational-review-queue-design.md
Section 2 (Non-Goals). Every approve/reject/rollback action a reviewer
takes still happens through the existing per-run endpoints already in
app/api/tasks.py -- this router adds no new write path of any kind.
"""
from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.api.deps import PaginationParams, get_current_active_user
from app.db.session import get_db
from app.models.user import User
from app.review_queue.query import ReviewQueueFilters, fetch_review_queue
from app.schemas.review_queue import (
    REVIEW_CATEGORIES,
    REVIEW_TYPES,
    ReviewQueueResponse,
    ReviewQueueSummary,
)

router = APIRouter(prefix="/review-queue", tags=["review-queue"])


@router.get("", response_model=ReviewQueueResponse)
def get_review_queue(
    review_category: list[str] | None = Query(default=None),
    review_type: list[str] | None = Query(default=None),
    source: list[str] | None = Query(default=None),
    search: str | None = Query(default=None),
    sort: str = Query(default="created_at"),
    pagination: PaginationParams = Depends(),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
) -> ReviewQueueResponse:
    # Validated here, at the API boundary, against the same small closed
    # vocabularies the pure query module trusts without re-validating --
    # matching this project's existing convention of validating enum-like
    # query params at the API layer (e.g. tasks.py's decision=duplicate/
    # ambiguous check) rather than inside the pure/reusable core module.
    if review_category is not None:
        invalid = [c for c in review_category if c not in REVIEW_CATEGORIES]
        if invalid:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid review_category value(s): {invalid}",
            )
    if review_type is not None:
        invalid = [t for t in review_type if t not in REVIEW_TYPES]
        if invalid:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"Invalid review_type value(s): {invalid}",
            )
    if sort not in ("created_at", "confidence_score"):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="sort must be 'created_at' or 'confidence_score'",
        )

    filters = ReviewQueueFilters(
        review_category=tuple(review_category) if review_category else None,
        review_type=tuple(review_type) if review_type else None,
        source=tuple(source) if source else None,
        search=search,
    )

    page = fetch_review_queue(
        session=db,
        organization_id=current_user.organization_id,
        filters=filters,
        sort=sort,
        limit=pagination.limit,
        offset=pagination.offset,
    )

    return ReviewQueueResponse(
        items=page.items,
        total=page.total,
        limit=pagination.limit,
        offset=pagination.offset,
        summary=ReviewQueueSummary(**page.summary),
    )
