"""
Authentication endpoints.

POST /auth/register  - create a new Organization + its first (admin) User
POST /auth/login      - organization_slug + email + password -> Token
GET  /auth/me         - the currently authenticated user
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_active_user
from app.core.security import create_access_token, hash_password, verify_password
from app.db.session import get_db
from app.models.organization import Organization
from app.models.user import User
from app.schemas.auth import LoginRequest, RegisterRequest, Token
from app.schemas.user import UserRead

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/register", response_model=Token, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, db: Session = Depends(get_db)) -> Token:
    slug = payload.resolved_slug()

    # Fast-path check for a friendly 409 without round-tripping to a DB
    # constraint violation in the common case.
    existing = db.execute(
        select(Organization.id).where(Organization.slug == slug)
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Organization slug '{slug}' is already taken",
        )

    organization = Organization(name=payload.organization_name, slug=slug)
    user = User(
        organization=organization,
        email=payload.email,
        hashed_password=hash_password(payload.password),
        full_name=payload.full_name,
        is_superuser=True,  # the first user of a new org is its admin
    )

    # Organization and User are added to the same session and committed
    # together. If the commit fails for any reason (including the race
    # condition where two requests register the same slug concurrently —
    # the unique constraint on organizations.slug is the real source of
    # truth, the SELECT above is just a fast path), the rollback below
    # undoes BOTH inserts. Neither row is ever persisted alone.
    db.add(organization)
    db.add(user)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Organization slug '{slug}' is already taken",
        )

    db.refresh(user)
    token = create_access_token(subject=user.id, organization_id=user.organization_id)
    return Token(access_token=token)


@router.post("/login", response_model=Token)
def login(payload: LoginRequest, db: Session = Depends(get_db)) -> Token:
    # One generic error for "org not found", "user not found in that org",
    # and "wrong password" — deliberately not distinguishing these to avoid
    # leaking which organizations/emails exist.
    invalid_credentials = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Invalid organization, email, or password",
    )

    organization = db.execute(
        select(Organization).where(Organization.slug == payload.organization_slug)
    ).scalar_one_or_none()
    if organization is None:
        raise invalid_credentials

    user = db.execute(
        select(User).where(
            User.organization_id == organization.id,
            User.email == payload.email,
        )
    ).scalar_one_or_none()
    if user is None:
        raise invalid_credentials

    if not verify_password(payload.password, user.hashed_password):
        raise invalid_credentials

    # Inactive accounts are a distinct, non-generic error: the credentials
    # were correct, the account is simply disabled.
    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user",
        )

    token = create_access_token(subject=user.id, organization_id=user.organization_id)
    return Token(access_token=token)


@router.get("/me", response_model=UserRead)
def read_current_user(current_user: User = Depends(get_current_active_user)) -> User:
    return current_user
