"""
Security primitives: password hashing, JWT issuance/verification, and
deterministic slug generation for organizations.

Nothing here talks to the database — this module is pure logic so it can be
unit tested in isolation.
"""
import base64
import hashlib
import hmac
import re
import secrets
import struct
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import bcrypt
import jwt
from cryptography.fernet import Fernet, InvalidToken

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
    auth_version: int = 1,
) -> str:
    settings = get_settings()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=settings.access_token_expire_minutes)
    )
    payload: dict[str, Any] = {
        "sub": str(subject),
        "org_id": str(organization_id),
        "exp": expire,
        "token_type": "access",
        "auth_version": auth_version,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def create_pre_auth_token(subject: uuid.UUID | str, organization_id: uuid.UUID | str, purpose: str, auth_version: int = 1) -> str:
    settings = get_settings()
    payload = {
        "sub": str(subject),
        "org_id": str(organization_id),
        "exp": datetime.now(timezone.utc) + timedelta(minutes=settings.pre_auth_token_expire_minutes),
        "token_type": "pre_auth",
        "purpose": purpose,
        "jti": secrets.token_urlsafe(16),
        "auth_version": auth_version,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=settings.jwt_algorithm)


def decode_access_token(token: str) -> dict[str, Any]:
    """Raises jwt.PyJWTError (or a subclass) on any invalid/expired token."""
    settings = get_settings()
    return jwt.decode(token, settings.secret_key, algorithms=[settings.jwt_algorithm])


def _totp_fernet() -> Fernet:
    settings = get_settings()
    key = settings.totp_encryption_key
    if not key:
        if settings.is_production:
            raise RuntimeError("TOTP_ENCRYPTION_KEY is required in production")
        key = base64.urlsafe_b64encode(hashlib.sha256(settings.secret_key.encode()).digest()).decode()
    return Fernet(key.encode())


def encrypt_totp_secret(secret: str) -> bytes:
    return _totp_fernet().encrypt(secret.encode("ascii"))


def decrypt_totp_secret(payload: bytes) -> str:
    try:
        return _totp_fernet().decrypt(payload).decode("ascii")
    except InvalidToken as exc:
        raise RuntimeError("Unable to decrypt TOTP secret") from exc


def generate_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii").rstrip("=")


def totp_code(secret: str, at_time: int | None = None) -> str:
    counter = int(at_time if at_time is not None else time.time()) // 30
    padded = secret + "=" * ((8 - len(secret) % 8) % 8)
    digest = hmac.new(base64.b32decode(padded), struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    value = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF) % 1_000_000
    return f"{value:06d}"


def verify_totp(secret: str, code: str, at_time: int | None = None) -> bool:
    if not code.isdigit() or len(code) != 6:
        return False
    now = int(at_time if at_time is not None else time.time())
    return any(hmac.compare_digest(totp_code(secret, now + offset * 30), code) for offset in (-1, 0, 1))


def generate_recovery_codes(count: int = 10) -> list[str]:
    return [f"{secrets.token_hex(6).upper()}-{secrets.token_hex(6).upper()}" for _ in range(count)]


def hash_recovery_code(code: str) -> str:
    normalized = code.strip().replace(" ", "").upper()
    return hmac.new(get_settings().secret_key.encode(), normalized.encode(), hashlib.sha256).hexdigest()


def hash_auth_identifier(value: str) -> str:
    return hmac.new(get_settings().secret_key.encode(), value.strip().lower().encode(), hashlib.sha256).hexdigest()


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
