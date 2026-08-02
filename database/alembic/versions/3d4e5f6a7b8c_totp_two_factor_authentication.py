"""TOTP two-factor authentication, recovery codes, and auth audit events.

Revision ID: 3d4e5f6a7b8c
Revises: 2c3d4e5f6a7b
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "3d4e5f6a7b8c"
down_revision: Union[str, None] = "2c3d4e5f6a7b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("users", sa.Column("totp_secret_encrypted", sa.LargeBinary(), nullable=True))
    op.add_column("users", sa.Column("totp_enabled", sa.Boolean(), server_default=sa.false(), nullable=False))
    op.add_column("users", sa.Column("totp_confirmed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("users", sa.Column("auth_version", sa.Integer(), server_default="1", nullable=False))

    op.create_table(
        "auth_recovery_codes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_hash"),
    )
    op.create_index("ix_auth_recovery_codes_user_id", "auth_recovery_codes", ["user_id"])

    op.create_table(
        "auth_security_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=True),
        sa.Column("organization_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(50), nullable=False),
        sa.Column("success", sa.Boolean(), nullable=False),
        sa.Column("identifier_hash", sa.String(64), nullable=True),
        sa.Column("ip_address", sa.String(45), nullable=True),
        sa.Column("detail", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["organization_id"], ["organizations.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_auth_security_events_user_id", "auth_security_events", ["user_id"])
    op.create_index("ix_auth_security_events_created_at", "auth_security_events", ["created_at"])
    op.create_index("ix_auth_events_identifier_created", "auth_security_events", ["identifier_hash", "created_at"])
    op.create_index("ix_auth_events_ip_created", "auth_security_events", ["ip_address", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_auth_events_ip_created", table_name="auth_security_events")
    op.drop_index("ix_auth_events_identifier_created", table_name="auth_security_events")
    op.drop_index("ix_auth_security_events_created_at", table_name="auth_security_events")
    op.drop_index("ix_auth_security_events_user_id", table_name="auth_security_events")
    op.drop_table("auth_security_events")
    op.drop_index("ix_auth_recovery_codes_user_id", table_name="auth_recovery_codes")
    op.drop_table("auth_recovery_codes")
    op.drop_column("users", "auth_version")
    op.drop_column("users", "totp_confirmed_at")
    op.drop_column("users", "totp_enabled")
    op.drop_column("users", "totp_secret_encrypted")
