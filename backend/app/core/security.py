"""
Security primitives: password hashing, JWT issuance/verification, and
deterministic slug generation for organizations.

Nothing here talks to the database — this module is pure logic so it can be
unit tested in isolation.
"""
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt

from app.core.config import get_settings

# --- Password hashing -------------------------------------------------------
# bcrypt has a hard 72-BYTE input limit (not 72 characters — a multi-byte
# UTF-8 character can push a shorter string over the limit). We validate this
# at the schema layer (never truncate silently) and treat any violation here
# as a programming error, not a user-facing one.
BCRYPT_MAX_BYTES = 72


def hash_password(password: str) -> str:
    password_bytes = password.encode("utf-8")
    if len(password_bytes) > BCRYPT_MAX_BYTES:
        # Should be unreachable if schema validation ran first; fail loudly
        # rather than silently truncating.
        raise ValueError(
            f"Password exceeds bcrypt's {BCRYPT_MAX_BYTES}-byte limit "
            f"({len(password_bytes)} bytes) — this should have been rejected "
            "by input validation before reaching hash_password()."
        )
    hashed = bcrypt.hashpw(password_bytes, bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(password: str, hashed_password: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode("utf-8"), hashed_password.encode("utf-8"))
    except ValueError:
        # Malformed hash in the DB — treat as a verification failure, not a
        # 500. This should never happen for hashes we generated ourselves.
        return False


# --- JWT ---------------------------------------------------------------------
def create_access_token(
    subject: uuid.UUID | str,
    organization_id: uuid.UUID | str,
    expires_delta: timedelta | None = None,
) -> str:
    settings = get_settings()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    )
    payload: dict[str, Any] = {
        "sub": str(subject),
        "org_id": str(organization_id),
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """Raises jwt.PyJWTError (or a subclass) on any invalid/expired token."""
    settings = get_settings()
    return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])


# --- Slug generation ----------------------------------------------------------
_SLUG_INVALID_CHARS = re.compile(r"[^a-z0-9]+")
_SLUG_COLLAPSE_DASHES = re.compile(r"-{2,}")
_SLUG_VALID_PATTERN = re.compile(r"^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?$")


def generate_slug(name: str) -> str:
    """
    Deterministic slug generation: the same input always produces the same
    output. Lowercase, trim, collapse any run of non-alphanumeric characters
    into a single hyphen, strip leading/trailing hyphens.

    This does NOT resolve collisions (e.g. no "-2" suffixing) — a colliding
    slug is a 409 Conflict at the API layer, never silently altered.
    """
    slug = name.strip().lower()
    slug = _SLUG_INVALID_CHARS.sub("-", slug)
    slug = _SLUG_COLLAPSE_DASHES.sub("-", slug)
    return slug.strip("-")


def is_valid_slug(slug: str) -> bool:
    """True if `slug` is already in canonical form (lowercase, a-z0-9-,
    no leading/trailing/doubled hyphens, non-empty)."""
    return bool(slug) and bool(_SLUG_VALID_PATTERN.match(slug)) and "--" not in slug


def normalize_email(email: str) -> str:
    return email.strip().lower()
