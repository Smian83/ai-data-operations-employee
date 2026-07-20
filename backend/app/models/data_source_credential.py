"""
DataSourceCredential: encrypted-at-rest storage for the live credentials a
DataSource needs to actually connect (as opposed to DataSource.
connection_metadata, which Module 3 restricts to non-secret parameters only
-- host, port, database name -- via a secret-key denylist scan).

This table is the *initial* CredentialProvider implementation's backing
store (see app.worker.credentials.DatabaseCredentialProvider). Nothing in
the execution engine or the CredentialProvider interface knows this table
exists; only DatabaseCredentialProvider does. Migrating to a managed
secrets service (Vault, AWS/GCP Secrets Manager) later means writing a new
CredentialProvider implementation and swapping it in -- no change to worker
logic, no change to this model's callers outside app.worker.credentials.

No ORM relationship exposes this table to any public read schema, and no
API endpoint ever returns its contents.
"""
import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, ForeignKeyConstraint, Integer, LargeBinary, func
from sqlalchemy import Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DataSourceCredential(Base):
    __tablename__ = "data_source_credentials"
    __table_args__ = (
        # One row per DataSource. Tenant-aware composite FK, same pattern as
        # Task -> DataSource and TaskRun -> Task.
        ForeignKeyConstraint(
            ["organization_id", "data_source_id"],
            ["data_sources.organization_id", "data_sources.id"],
            name="fk_data_source_credentials_org_data_source",
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    data_source_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), nullable=False, unique=True, index=True
    )
    # Fernet-encrypted (AES-128-CBC + HMAC) JSON blob of the actual
    # credentials. Never decrypted anywhere except inside
    # DatabaseCredentialProvider.get_credentials(), and never logged.
    encrypted_payload: Mapped[bytes] = mapped_column(LargeBinary(), nullable=False)
    # Which app-config key version encrypted this row, so the encryption key
    # can be rotated without breaking previously-stored rows.
    key_version: Mapped[int] = mapped_column(Integer(), nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"DataSourceCredential(data_source={self.data_source_id!r})"
