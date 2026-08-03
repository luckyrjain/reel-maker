"""Add tiktok platform value and credential OAuth bookkeeping columns.

Revision ID: 0004
Revises: 0003
Create Date: 2026-08-02
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ALTER TYPE ADD VALUE cannot run inside a transaction on PG <12;
    # autocommit_block() ensures it runs outside the implicit transaction.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE cutplatform ADD VALUE IF NOT EXISTS 'tiktok'")

    # Provider-specific IDs discovered during OAuth (e.g. the Instagram Business
    # Account ID behind a connected Facebook Page) that don't fit `scopes`.
    op.add_column("credentials", sa.Column("provider_account_id", sa.String(255), nullable=True))
    op.add_column("credentials", sa.Column("refresh_token_blob", sa.Text(), nullable=True))


def downgrade() -> None:
    # PostgreSQL does not support DROP VALUE from an enum type without recreating it.
    # Enum value removal is intentionally skipped here — acceptable for dev environments.
    op.drop_column("credentials", "refresh_token_blob")
    op.drop_column("credentials", "provider_account_id")
