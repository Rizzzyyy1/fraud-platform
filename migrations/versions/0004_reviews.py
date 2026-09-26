"""Analyst reviews (append-only), console job records, and an index for review-queue queries.

Reviews are a separate, append-only record of analyst work. They never modify `decisions`, and
an analyst disposition is not a confirmed fraud label. `console_jobs` records the bounded,
predefined demo jobs; a partial unique index allows at most one active job at a time.

Revision ID: 0004
Revises: 0003
"""

from __future__ import annotations

from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
    CREATE TABLE reviews (
        id             bigserial PRIMARY KEY,
        transaction_id text NOT NULL REFERENCES decisions (transaction_id),
        status         text NOT NULL CHECK (status IN ('open', 'in_review', 'closed')),
        disposition    text CHECK (disposition IN
                           ('suspected_fraud', 'likely_legitimate', 'needs_more_information')),
        note           text CHECK (note IS NULL OR length(note) <= 2000),
        analyst        text NOT NULL CHECK (length(analyst) BETWEEN 1 AND 64),
        created_at     timestamptz NOT NULL DEFAULT clock_timestamp()
    );
    CREATE INDEX reviews_transaction_idx ON reviews (transaction_id, id DESC);
    CREATE INDEX decisions_action_time_idx ON decisions (action, decision_time DESC);
    CREATE TABLE console_jobs (
        id           bigserial PRIMARY KEY,
        kind         text NOT NULL CHECK (kind IN ('traffic', 'failure_drill')),
        params       jsonb NOT NULL,
        namespace    text NOT NULL,
        status       text NOT NULL
                     CHECK (status IN ('running', 'succeeded', 'failed', 'cancelled')),
        requested_by text NOT NULL,
        pid          integer,
        start_offset integer NOT NULL,
        sent         integer NOT NULL DEFAULT 0,
        outcomes     jsonb NOT NULL DEFAULT '{}'::jsonb,
        message      text,
        created_at   timestamptz NOT NULL DEFAULT clock_timestamp(),
        finished_at  timestamptz
    );
    CREATE UNIQUE INDEX console_jobs_one_active ON console_jobs ((true)) WHERE status = 'running';
    CREATE RULE reviews_no_update AS ON UPDATE TO reviews DO INSTEAD NOTHING;
    CREATE RULE reviews_no_delete AS ON DELETE TO reviews DO INSTEAD NOTHING;
    """)


def downgrade() -> None:
    op.execute("""
    DROP RULE reviews_no_delete ON reviews;
    DROP RULE reviews_no_update ON reviews;
    DROP TABLE console_jobs;
    DROP INDEX decisions_action_time_idx;
    DROP TABLE reviews;
    """)
