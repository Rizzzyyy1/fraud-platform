"""Decisions audit table and transactional outbox.

Revision ID: 0001
Revises:
"""

from __future__ import annotations

from alembic import op

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE decisions (
        transaction_id    text PRIMARY KEY,
        request_hash      bytea NOT NULL CHECK (octet_length(request_hash) = 32),
        hash_version      smallint NOT NULL,
        customer_id       text NOT NULL,
        terminal_id       text NOT NULL,
        amount_minor      bigint NOT NULL CHECK (amount_minor >= 0),
        currency          char(3) NOT NULL,
        event_time        timestamptz NOT NULL,
        received_at       timestamptz NOT NULL,
        decision_time     timestamptz NOT NULL,
        persisted_at      timestamptz NOT NULL DEFAULT clock_timestamp(),
        score             double precision CHECK (score IS NULL OR (score >= 0 AND score <= 1)),
        action            text NOT NULL CHECK (action IN ('approve', 'review', 'decline')),
        reason_codes      text[] NOT NULL,
        features          jsonb NOT NULL,
        feature_freshness jsonb NOT NULL,
        model_version     text,
        feature_version   text NOT NULL,
        policy_version    text NOT NULL
    );
    CREATE INDEX decisions_decision_time_idx ON decisions (decision_time DESC);
    CREATE INDEX decisions_customer_event_time_idx ON decisions (customer_id, event_time);

    CREATE TABLE outbox (
        id             bigserial PRIMARY KEY,
        event_id       uuid NOT NULL UNIQUE,
        aggregate_id   text NOT NULL,
        event_type     text NOT NULL,
        schema_version integer NOT NULL,
        payload        jsonb NOT NULL,
        created_at     timestamptz NOT NULL DEFAULT clock_timestamp(),
        published_at   timestamptz,
        UNIQUE (aggregate_id, event_type)
    );
    CREATE INDEX outbox_unpublished_idx ON outbox (id) WHERE published_at IS NULL;
    """)


def downgrade() -> None:
    op.execute("DROP TABLE outbox; DROP TABLE decisions;")
