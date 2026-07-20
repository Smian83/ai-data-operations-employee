"""
Application-layer envelope encryption for DataSourceCredential rows.

Pure logic, no database access -- kept separate from
app.worker.credentials so the encryption primitive itself can be unit
tested in isolation, same rationale as app.core.security for passwords/JWTs.

Uses Fernet (AES-128-CBC + HMAC-SHA256, from the `cryptography` package):
authenticated encryption, safe defaults, no parameter choices to get wrong.
This is explicitly an interim, MVP-grade secret store -- see
app.worker.credentials module docstring for the production recommendation
(migrate to a managed secrets service; nothing outside this module and
app.worker.credentials needs to change when that happens).
"""
import json
import logging

from cryptography.fernet import Fernet, InvalidToken

from app.core.config import get_settings

logger = logging.getLogger(__name__)

CURRENT_KEY_VERSION = 1


class CredentialEncryptionError(Exception):
    """Raised when credentials cannot be encrypted or decrypted -- e.g. the
    encryption key is missing/misconfigured, or a stored blob was tampered
    with or encrypted under a key we no longer have."""


def _get_fernet() -> Fernet:
    settings = get_settings()
    key = settings.credential_encryption_key
    if not key:
        if settings.is_production:
            raise CredentialEncryptionError(
                "CREDENTIAL_ENCRYPTION_KEY is not set. Refusing to handle "
                "DataSource credentials in production without an encryption "
                "key configured."
            )
        # Sandbox/dev-only fallback so local testing doesn't require a key
        # to be generated first. NEVER reached in production (guarded above).
        logger.warning(
            "CREDENTIAL_ENCRYPTION_KEY is unset -- using an insecure, "
            "process-local dev fallback key. This must never happen outside "
            "local development."
        )
        key = "ZGV2LW9ubHktaW5zZWN1cmUtZmVybmV0LWtleS0zMmI="  # 32 zero-ish bytes, dev only
    try:
        return Fernet(key.encode("utf-8") if isinstance(key, str) else key)
    except (ValueError, TypeError) as exc:
        raise CredentialEncryptionError(f"Malformed CREDENTIAL_ENCRYPTION_KEY: {exc}") from exc


def encrypt_credentials(credentials: dict) -> bytes:
    """Serialize `credentials` to JSON and encrypt it. Raises
    CredentialEncryptionError on any failure (never silently returns
    plaintext)."""
    try:
        plaintext = json.dumps(credentials).encode("utf-8")
        return _get_fernet().encrypt(plaintext)
    except CredentialEncryptionError:
        raise
    except (TypeError, ValueError) as exc:
        raise CredentialEncryptionError(f"Could not encrypt credentials: {exc}") from exc


def decrypt_credentials(encrypted_payload: bytes) -> dict:
    """Decrypt and deserialize a credentials blob. Raises
    CredentialEncryptionError on any failure -- including a wrong/rotated
    key or tampered ciphertext -- never returns partial/garbage data."""
    try:
        plaintext = _get_fernet().decrypt(encrypted_payload)
        return json.loads(plaintext.decode("utf-8"))
    except InvalidToken as exc:
        raise CredentialEncryptionError(
            "Could not decrypt credentials -- wrong key or corrupted data."
        ) from exc
    except (CredentialEncryptionError,):
        raise
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise CredentialEncryptionError(f"Could not decrypt credentials: {exc}") from exc
