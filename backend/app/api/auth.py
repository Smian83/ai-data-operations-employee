"""Registration, password authentication, and authenticator-app TOTP flows."""
from datetime import datetime, timedelta, timezone
from urllib.parse import quote
import uuid

import jwt
from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_current_active_user
from app.core.config import get_settings
from app.core.security import (
    create_access_token, create_pre_auth_token, decode_access_token,
    decrypt_totp_secret, encrypt_totp_secret, generate_recovery_codes,
    generate_totp_secret, hash_auth_identifier, hash_password,
    hash_recovery_code, verify_password, verify_totp,
)
from app.db.session import get_db
from app.models.auth_security import AuthRecoveryCode, AuthSecurityEvent
from app.models.organization import Organization
from app.models.user import User
from app.schemas.auth import (
    AuthResponse, LoginRequest, RecoveryCodeRequest, RecoveryCodesResponse,
    RegisterRequest, TotpCodeRequest, TwoFactorManageRequest,
    TwoFactorSetupResponse, TwoFactorStatusResponse, TwoFactorVerifyResponse,
)
from app.schemas.user import UserRead

router = APIRouter(prefix="/auth", tags=["auth"])
bearer = HTTPBearer(auto_error=True)


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _audit(db: Session, request: Request, event_type: str, success: bool, *, user: User | None = None, identifier: str | None = None, detail: str | None = None) -> None:
    db.add(AuthSecurityEvent(
        user_id=user.id if user else None,
        organization_id=user.organization_id if user else None,
        event_type=event_type,
        success=success,
        identifier_hash=hash_auth_identifier(identifier) if identifier else None,
        ip_address=_ip(request),
        detail=detail,
    ))


def _enforce_rate_limit(db: Session, request: Request, event_type: str, identifier: str) -> None:
    settings = get_settings()
    since = datetime.now(timezone.utc) - timedelta(minutes=settings.auth_rate_limit_window_minutes)
    identifier_hash = hash_auth_identifier(identifier)
    filters = [AuthSecurityEvent.identifier_hash == identifier_hash]
    if _ip(request):
        filters.append(AuthSecurityEvent.ip_address == _ip(request))
    attempts = db.scalar(select(func.count()).select_from(AuthSecurityEvent).where(
        AuthSecurityEvent.event_type == event_type,
        AuthSecurityEvent.success.is_(False),
        AuthSecurityEvent.created_at >= since,
        or_(*filters),
    )) or 0
    if attempts >= settings.auth_rate_limit_attempts:
        raise HTTPException(status_code=429, detail="Too many authentication attempts. Try again later.", headers={"Retry-After": str(settings.auth_rate_limit_window_minutes * 60)})


def _full_token(user: User) -> str:
    return create_access_token(user.id, user.organization_id, auth_version=user.auth_version)


def _pre_token(user: User, purpose: str) -> str:
    return create_pre_auth_token(user.id, user.organization_id, purpose, user.auth_version)


def _auth_response(user: User) -> AuthResponse:
    if user.is_superuser and not user.totp_enabled:
        return AuthResponse(requires_2fa_setup=True, pre_auth_token=_pre_token(user, "setup"))
    if user.totp_enabled:
        return AuthResponse(requires_2fa=True, pre_auth_token=_pre_token(user, "verify"))
    return AuthResponse(access_token=_full_token(user))


def _token_user(credentials: HTTPAuthorizationCredentials, db: Session, purposes: set[str]) -> User:
    try:
        payload = decode_access_token(credentials.credentials)
    except jwt.PyJWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired authentication token")
    token_type = payload.get("token_type", "access")
    if token_type == "pre_auth":
        if payload.get("purpose") not in purposes:
            raise HTTPException(status_code=401, detail="Authentication token is not valid for this operation")
    elif token_type != "access" or "access" not in purposes:
        raise HTTPException(status_code=401, detail="Authentication token is not valid for this operation")
    try:
        user_id = uuid.UUID(str(payload.get("sub")))
    except (ValueError, TypeError):
        raise HTTPException(status_code=401, detail="Invalid or expired authentication token")
    user = db.get(User, user_id)
    if not user or not user.is_active or str(user.organization_id) != str(payload.get("org_id")) or payload.get("auth_version", 1) != user.auth_version:
        raise HTTPException(status_code=401, detail="Invalid or expired authentication token")
    return user


def _replace_recovery_codes(db: Session, user: User) -> list[str]:
    codes = generate_recovery_codes()
    db.execute(delete(AuthRecoveryCode).where(AuthRecoveryCode.user_id == user.id))
    db.add_all(AuthRecoveryCode(user_id=user.id, code_hash=hash_recovery_code(code)) for code in codes)
    return codes


@router.post("/register", response_model=AuthResponse, status_code=status.HTTP_201_CREATED)
def register(payload: RegisterRequest, request: Request, db: Session = Depends(get_db)) -> AuthResponse:
    slug = payload.resolved_slug()
    if db.scalar(select(Organization.id).where(Organization.slug == slug)) is not None:
        raise HTTPException(status_code=409, detail=f"Organization slug '{slug}' is already taken")
    organization = Organization(name=payload.organization_name, slug=slug)
    user = User(organization=organization, email=payload.email, hashed_password=hash_password(payload.password), full_name=payload.full_name, is_superuser=True)
    db.add_all([organization, user])
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail=f"Organization slug '{slug}' is already taken")
    db.refresh(user)
    _audit(db, request, "register", True, user=user)
    db.commit()
    return _auth_response(user)


@router.post("/login", response_model=AuthResponse)
def login(payload: LoginRequest, request: Request, db: Session = Depends(get_db)) -> AuthResponse:
    identifier = f"{payload.organization_slug}:{payload.email}"
    _enforce_rate_limit(db, request, "login", identifier)
    organization = db.scalar(select(Organization).where(Organization.slug == payload.organization_slug))
    user = db.scalar(select(User).where(User.organization_id == organization.id, User.email == payload.email)) if organization else None
    if user is None or not verify_password(payload.password, user.hashed_password):
        _audit(db, request, "login", False, identifier=identifier, detail="invalid_credentials")
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid organization, email, or password")
    if not user.is_active:
        _audit(db, request, "login", False, user=user, identifier=identifier, detail="inactive_user")
        db.commit()
        raise HTTPException(status_code=403, detail="Inactive user")
    _audit(db, request, "login", True, user=user, identifier=identifier)
    db.commit()
    return _auth_response(user)


@router.get("/me", response_model=UserRead)
def read_current_user(current_user: User = Depends(get_current_active_user)) -> User:
    return current_user


@router.post("/2fa/setup", response_model=TwoFactorSetupResponse)
def setup_2fa(request: Request, credentials: HTTPAuthorizationCredentials = Depends(bearer), db: Session = Depends(get_db)) -> TwoFactorSetupResponse:
    user = _token_user(credentials, db, {"setup", "access"})
    if user.totp_enabled:
        raise HTTPException(status_code=409, detail="Two-factor authentication is already enabled")
    secret = generate_totp_secret()
    user.totp_secret_encrypted = encrypt_totp_secret(secret)
    issuer = get_settings().app_name
    label = quote(f"{issuer}:{user.email}")
    uri = f"otpauth://totp/{label}?secret={secret}&issuer={quote(issuer)}&algorithm=SHA1&digits=6&period=30"
    _audit(db, request, "2fa_setup_started", True, user=user)
    db.commit()
    return TwoFactorSetupResponse(secret=secret, provisioning_uri=uri)


@router.post("/2fa/setup/verify", response_model=TwoFactorVerifyResponse)
def verify_2fa_setup(payload: TotpCodeRequest, request: Request, credentials: HTTPAuthorizationCredentials = Depends(bearer), db: Session = Depends(get_db)) -> TwoFactorVerifyResponse:
    user = _token_user(credentials, db, {"setup", "access"})
    if user.totp_enabled:
        raise HTTPException(status_code=409, detail="Two-factor authentication is already enabled")
    _enforce_rate_limit(db, request, "2fa_setup_verify", str(user.id))
    if not user.totp_secret_encrypted or not verify_totp(decrypt_totp_secret(user.totp_secret_encrypted), payload.code):
        _audit(db, request, "2fa_setup_verify", False, user=user, identifier=str(user.id), detail="invalid_code")
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid authentication code")
    user.totp_enabled = True
    user.totp_confirmed_at = datetime.now(timezone.utc)
    user.auth_version += 1
    codes = _replace_recovery_codes(db, user)
    _audit(db, request, "2fa_enabled", True, user=user)
    db.commit()
    return TwoFactorVerifyResponse(access_token=_full_token(user), recovery_codes=codes)


@router.post("/2fa/verify", response_model=TwoFactorVerifyResponse)
def verify_2fa(payload: TotpCodeRequest, request: Request, credentials: HTTPAuthorizationCredentials = Depends(bearer), db: Session = Depends(get_db)) -> TwoFactorVerifyResponse:
    user = _token_user(credentials, db, {"verify"})
    _enforce_rate_limit(db, request, "2fa_verify", str(user.id))
    if not user.totp_enabled or not user.totp_secret_encrypted or not verify_totp(decrypt_totp_secret(user.totp_secret_encrypted), payload.code):
        _audit(db, request, "2fa_verify", False, user=user, identifier=str(user.id), detail="invalid_code")
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid authentication code")
    _audit(db, request, "2fa_verify", True, user=user, identifier=str(user.id))
    db.commit()
    return TwoFactorVerifyResponse(access_token=_full_token(user))


@router.post("/2fa/recovery", response_model=TwoFactorVerifyResponse)
def recover_2fa(payload: RecoveryCodeRequest, request: Request, credentials: HTTPAuthorizationCredentials = Depends(bearer), db: Session = Depends(get_db)) -> TwoFactorVerifyResponse:
    user = _token_user(credentials, db, {"verify"})
    _enforce_rate_limit(db, request, "2fa_recovery", str(user.id))
    code_hash = hash_recovery_code(payload.recovery_code)
    result = db.execute(update(AuthRecoveryCode).where(AuthRecoveryCode.user_id == user.id, AuthRecoveryCode.code_hash == code_hash, AuthRecoveryCode.used_at.is_(None)).values(used_at=datetime.now(timezone.utc)))
    if result.rowcount != 1:
        _audit(db, request, "2fa_recovery", False, user=user, identifier=str(user.id), detail="invalid_code")
        db.commit()
        raise HTTPException(status_code=401, detail="Invalid or already used recovery code")
    _audit(db, request, "2fa_recovery", True, user=user, identifier=str(user.id))
    db.commit()
    return TwoFactorVerifyResponse(access_token=_full_token(user))


@router.get("/2fa/status", response_model=TwoFactorStatusResponse)
def two_factor_status(current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)) -> TwoFactorStatusResponse:
    remaining = db.scalar(select(func.count()).select_from(AuthRecoveryCode).where(AuthRecoveryCode.user_id == current_user.id, AuthRecoveryCode.used_at.is_(None))) or 0
    return TwoFactorStatusResponse(enabled=current_user.totp_enabled, required=current_user.is_superuser, recovery_codes_remaining=remaining)


@router.post("/2fa/recovery-codes/regenerate", response_model=RecoveryCodesResponse)
def regenerate_recovery_codes(payload: TwoFactorManageRequest, request: Request, current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)) -> RecoveryCodesResponse:
    _enforce_rate_limit(db, request, "2fa_recovery_codes_regenerated", str(current_user.id))
    if not current_user.totp_enabled or not current_user.totp_secret_encrypted or not verify_password(payload.password, current_user.hashed_password) or not verify_totp(decrypt_totp_secret(current_user.totp_secret_encrypted), payload.code):
        _audit(db, request, "2fa_recovery_codes_regenerated", False, user=current_user, detail="verification_failed")
        db.commit()
        raise HTTPException(status_code=401, detail="Password or authentication code is invalid")
    codes = _replace_recovery_codes(db, current_user)
    _audit(db, request, "2fa_recovery_codes_regenerated", True, user=current_user)
    db.commit()
    return RecoveryCodesResponse(recovery_codes=codes)


@router.post("/2fa/disable", response_model=TwoFactorStatusResponse)
def disable_2fa(payload: TwoFactorManageRequest, request: Request, current_user: User = Depends(get_current_active_user), db: Session = Depends(get_db)) -> TwoFactorStatusResponse:
    _enforce_rate_limit(db, request, "2fa_disabled", str(current_user.id))
    if current_user.is_superuser:
        _audit(db, request, "2fa_disabled", False, user=current_user, identifier=str(current_user.id), detail="required_for_admin")
        db.commit()
        raise HTTPException(status_code=403, detail="Two-factor authentication is required for organization administrators")
    if not current_user.totp_enabled or not current_user.totp_secret_encrypted or not verify_password(payload.password, current_user.hashed_password) or not verify_totp(decrypt_totp_secret(current_user.totp_secret_encrypted), payload.code):
        _audit(db, request, "2fa_disabled", False, user=current_user, detail="verification_failed")
        db.commit()
        raise HTTPException(status_code=401, detail="Password or authentication code is invalid")
    current_user.totp_enabled = False
    current_user.totp_secret_encrypted = None
    current_user.totp_confirmed_at = None
    current_user.auth_version += 1
    db.execute(delete(AuthRecoveryCode).where(AuthRecoveryCode.user_id == current_user.id))
    _audit(db, request, "2fa_disabled", True, user=current_user)
    db.commit()
    return TwoFactorStatusResponse(enabled=False, required=False, recovery_codes_remaining=0)
