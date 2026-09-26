"""Alembic environment. Migrations are plain SQL; no ORM models are used."""

from __future__ import annotations

from alembic import context
from sqlalchemy import create_engine

from fraudplat.config import Settings


def _database_url() -> str:
    override = context.get_x_argument(as_dictionary=True).get("url")
    url = override or Settings().database_url.get_secret_value()  # type: ignore[call-arg]
    return url.replace("postgresql://", "postgresql+psycopg://", 1)


engine = create_engine(_database_url())
with engine.connect() as connection:
    context.configure(connection=connection)
    with context.begin_transaction():
        context.run_migrations()
engine.dispose()
