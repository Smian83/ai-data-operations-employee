"""User model. Every user belongs to exactly one Organization (tenant)."""
import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, LargeBinary, String, UniqueConstraint, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        UniqueConstraint("organization_id", "email", name="uq_users_org_email"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), primary_key=True, default=uuid.uuid4
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(), ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # Normalized (lowercase, trimmed) email. Unique PER ORGANIZATION only —
    # see uq_users_org_email above. Do NOT add a global unique constraint here.
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    hashed_password: Mapped[str] = mapped_column(String(255), nullable=False)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=True)
    is_superuser: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    totp_secret_encrypted: Mapped[bytes | None] = mapped_column(LargeBinary(), nullable=True)
    totp_enabled: Mapped[bool] = mapped_column(Boolean(), nullable=False, default=False)
    totp_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    auth_version: Mapped[int] = mapped_column(Integer(), nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    organization: Mapped["Organization"] = relationship(back_populates="users")  # noqa: F821

    def __repr__(self) -> str:
        return f"User(id={self.id!r}, org={self.organization_id!r}, email={self.email!r})"
