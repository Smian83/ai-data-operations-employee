"""
Shared FastAPI dependencies for authentication.

get_current_active_user is what every future protected endpoint (Module 3+)
should depend on to get the authenticated, tenant-scoped user.
"""
import logging
import uuid

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from sqlalchemy.orm import Session

from app.core.security import decode_access_token
from app.db.session import get_db
from app.models.user import User

logger = logging.getLogger(__name__)

# tokenUrl is only used to populate the Swagger "Authorize" UI — the actual
# login endpoint accepts a JSON body (organization_slug + email + password),
# not an OAuth2 form, because OAuth2PasswordRequestForm has no room for a
# third (tenant-scoping) field.
oauth2_scheme = OAuth2PasswordBearer(tokenUrl="auth/login", auto_error=True)

_CREDENTIALS_EXCEPTION = HTTPException(
    status_code=status.HTTP_401_UNAUTHORIZED,
    detail="Could not validate credentials",
    headers={"WWW-Authenticate": "Bearer"},
)


def get_current_user(
    token: str = Depends(oauth2_scheme),
    db: Session = Depends(get_db),
) -> User:
    try:
        payload = decode_access_token(token)
    except jwt.PyJWTError:
        raise _CREDENTIALS_EXCEPTION

    raw_user_id = payload.get("sub")
    org_id = payload.get("org_id")
    if not raw_user_id or not org_id:
        raise _CREDENTIALS_EXCEPTION

    try:
        user_id = uuid.UUID(raw_user_id)
    except (ValueError, AttributeError, TypeError):
        raise _CREDENTIALS_EXCEPTION

    user = db.get(User, user_id)
    if user is None:
        raise _CREDENTIALS_EXCEPTION

    # Defense in depth: the token's org_id must match the loaded user's
    # current organization_id. This catches tokens issued before a
    # (hypothetical future) org transfer, and any token tampering that
    # swapped sub but not org_id.
    if str(user.organization_id) != str(org_id):
        logger.warning(
            "Token org_id mismatch for user %s: token=%s actual=%s",
            user_id, org_id, user.organization_id,
        )
        raise _CREDENTIALS_EXCEPTION

    return user


def get_current_active_user(current_user: User = Depends(get_current_user)) -> User:
    if not current_user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Inactive user",
        )
    return current_user
