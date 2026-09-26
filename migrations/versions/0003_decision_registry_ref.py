"""Record which registry version (name/version) served each decision.

Revision ID: 0003
Revises: 0002
"""

from __future__ import annotations

from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("ALTER TABLE decisions ADD COLUMN model_registry_ref text")


def downgrade() -> None:
    op.execute("ALTER TABLE decisions DROP COLUMN model_registry_ref")
