"""Add enriched_context column; add enriching/enrich enum values.

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-25
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("reels", sa.Column("enriched_context", sa.Text(), nullable=True))
    # ALTER TYPE ADD VALUE cannot run inside a transaction on PG <12;
    # autocommit_block() ensures it runs outside the implicit transaction.
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE reelstatus ADD VALUE IF NOT EXISTS 'enriching'")
        op.execute("ALTER TYPE jobtype ADD VALUE IF NOT EXISTS 'enrich'")


def downgrade() -> None:
    # PostgreSQL does not support DROP VALUE from an enum type without recreating it.
    # Enum value removal is intentionally skipped here — acceptable for dev environments.
    # To fully revert: recreate reelstatus without 'enriching' and jobtype without 'enrich'.
    op.drop_column("reels", "enriched_context")
