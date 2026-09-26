"""Outbox rows carry the stream (feature namespace) they belong to.

Revision ID: 0002
Revises: 0001
"""

from __future__ import annotations

from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    ALTER TABLE outbox ADD COLUMN stream text NOT NULL DEFAULT 'live'
        CHECK (stream = 'live' OR stream ~ '^replay:[a-z0-9][a-z0-9-]*$');
    DROP INDEX outbox_unpublished_idx;
    CREATE INDEX outbox_unpublished_idx ON outbox (stream, id) WHERE published_at IS NULL;
    """)


def downgrade() -> None:
    op.execute("""
    DROP INDEX outbox_unpublished_idx;
    ALTER TABLE outbox DROP COLUMN stream;
    CREATE INDEX outbox_unpublished_idx ON outbox (id) WHERE published_at IS NULL;
    """)
