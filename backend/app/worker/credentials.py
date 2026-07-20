"""
CredentialProvider: the abstraction the execution engine uses to resolve a
DataSource's live credentials at execution time.

The engine and every execution handler depend ONLY on the
CredentialProvider protocol below (`get_credentials(data_source) -> dict`).
Nothing in app.worker.engine or app.worker.handlers imports
DataSourceCredential, the encryption module, or any storage detail directly.

DatabaseCredentialProvider is the initial (MVP) implementation, backed by
the encrypted `data_source_credentials` table (app.core.encryption +
app.models.data_source_credential). Migrating to a managed secrets service
(HashiCorp Vault, AWS Secrets Manager, GCP Secret Manager) later means
writing e.g. a VaultCredentialProvider that implements the same protocol
and swapping the instance constructed in app.worker.runner -- no change to
engine or handler code, because they were never told where secrets live.

Credentials are resolved just-in-time (never cached beyond a single
execution) and are never logged. Callers must not persist the returned
dict anywhere other than local variables scoped to a single handler
invocation.
"""
import logging
import uuid
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.encryption import (
    CURRENT_KEY_VERSION,
    CredentialEncryptionError,
    decrypt_credentials,
    encrypt_credentials,
)
from app.models.data_source import DataSource
from app.models.data_source_credential import DataSourceCredential

logger = logging.getLogger(__name__)


class CredentialNotFoundError(Exception):
    """Raised when a DataSource has no stored credentials. This is a
    distinct, actionable error -- not the same as a decryption failure --
    so handlers/operators can tell "nothing was ever configured" apart
    from "something is configured but broken"."""


class CredentialProvider(Protocol):
    """The only interface the execution engine and handlers are allowed to
    depend on for secret retrieval."""

    def get_credentials(self, data_source: DataSource) -> dict:
        """Return the live credentials for `data_source` as a plain dict.
        Raises CredentialNotFoundError if none are configured."""
        ...


class DatabaseCredentialProvider:
    """MVP CredentialProvider backed by the encrypted
    `data_source_credentials` table. See module docstring for the intended
    migration path away from this implementation."""

    def __init__(self, db: Session) -> None:
        self._db = db

    def get_credentials(self, data_source: DataSource) -> dict:
        row = self._db.execute(
            select(DataSourceCredential).where(
                DataSourceCredential.organization_id == data_source.organization_id,
                DataSourceCredential.data_source_id == data_source.id,
            )
        ).scalar_one_or_none()
        if row is None:
            raise CredentialNotFoundError(
                f"No credentials configured for data source {data_source.id}"
            )
        try:
            return decrypt_credentials(row.encrypted_payload)
        except CredentialEncryptionError:
            logger.error(
                "Failed to decrypt credentials for data source %s (key_version=%s)",
                data_source.id, row.key_version,
            )
            raise

    def set_credentials(
        self,
        organization_id: uuid.UUID,
        data_source_id: uuid.UUID,
        credentials: dict,
    ) -> DataSourceCredential:
        """Encrypt and upsert credentials for a DataSource. Used by the
        (admin-only) API write path -- never by the execution engine
        itself, which only ever reads."""
        encrypted = encrypt_credentials(credentials)
        existing = self._db.execute(
            select(DataSourceCredential).where(
                DataSourceCredential.organization_id == organization_id,
                DataSourceCredential.data_source_id == data_source_id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            existing.encrypted_payload = encrypted
            existing.key_version = CURRENT_KEY_VERSION
            return existing
        row = DataSourceCredential(
            organization_id=organization_id,
            data_source_id=data_source_id,
            encrypted_payload=encrypted,
            key_version=CURRENT_KEY_VERSION,
        )
        self._db.add(row)
        return row
