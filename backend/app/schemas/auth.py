"""
Auth request/response schemas.

All normalization (email lowercasing/trimming, slug lowercasing/trimming)
happens HERE, at the schema boundary, so every layer beneath (CRUD, DB
queries, JWT payloads) can assume email/slug values are already canonical.
"""
import uuid

from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.security import generate_slug, is_valid_slug, normalize_email

MIN_PASSWORD_LENGTH = 8
# bcrypt's hard limit is 72 BYTES, not characters — a password full of
# multi-byte UTF-8 characters can exceed this well under 72 characters.
MAX_PASSWORD_BYTES = 72


def _validate_password_strength(password: str) -> str:
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long")
    encoded_len = len(password.encode("utf-8"))
    if encoded_len > MAX_PASSWORD_BYTES:
        raise ValueError(
            f"Password must not exceed {MAX_PASSWORD_BYTES} bytes when UTF-8 "
            f"encoded (got {encoded_len} bytes). Passwords are never "
            "truncated — please choose a shorter password."
        )
    return password


class RegisterRequest(BaseModel):
    organization_name: str = Field(min_length=1, max_length=255)
    # If omitted, the slug is deterministically derived from organization_name.
    organization_slug: str | None = Field(default=None, max_length=255)
    email: EmailStr
    password: str
    full_name: str | None = Field(default=None, max_length=255)

    @field_validator("organization_name")
    @classmethod
    def _strip_org_name(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("organization_name must not be blank")
        return v

    @field_validator("organization_slug")
    @classmethod
    def _normalize_org_slug(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip().lower()
        if not is_valid_slug(v):
            raise ValueError(
                "organization_slug must be lowercase alphanumeric characters "
                "separated by single hyphens (e.g. 'acme-corp')"
            )
        return v

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return normalize_email(v)

    @field_validator("password")
    @classmethod
    def _validate_password(cls, v: str) -> str:
        return _validate_password_strength(v)

    def resolved_slug(self) -> str:
        """The slug to actually use: explicit organization_slug if given,
        otherwise deterministically generated from organization_name."""
        return self.organization_slug or generate_slug(self.organization_name)


class LoginRequest(BaseModel):
    organization_slug: str
    email: EmailStr
    password: str

    @field_validator("organization_slug")
    @classmethod
    def _normalize_slug(cls, v: str) -> str:
        return v.strip().lower()

    @field_validator("email")
    @classmethod
    def _normalize_email(cls, v: str) -> str:
        return normalize_email(v)


class Token(BaseModel):
    access_token: str
    token_type: str = "bearer"


class TokenPayload(BaseModel):
    sub: uuid.UUID
    org_id: uuid.UUID
    exp: int
