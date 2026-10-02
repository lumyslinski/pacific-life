"""Alembic environment of the GEA PostgreSQL schema.

SQL-first: the revisions run hand-written SQL (see gea_sql.py), so there is no
ORM metadata and `alembic revision --autogenerate` is not used.

    export GEA_DATABASE_URL=postgresql://gea_owner:secret@localhost:5432/gea
    alembic -c gea/db/postgres/alembic.ini upgrade head
    alembic -c gea/db/postgres/alembic.ini upgrade head --sql     # print the SQL instead

The URL can also be given per call: `alembic -x url=postgresql://... upgrade head`.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import create_engine, pool, text
from sqlalchemy.engine import URL, make_url

sys.path.insert(0, str(Path(__file__).resolve().parent))    # the revisions import gea_sql

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

SCHEMA = "gea"
# Alembic's own bookkeeping table, named like the other tables of the schema.
VERSION_TABLE = "AlembicVersion"
# One deployment migrates at a time; a second one waits on this advisory lock,
# then finds nothing left to do.
LOCK_KEY = 4739201774
# DDL that cannot get its lock within this time fails instead of queueing behind
# a long transaction and blocking every other session on the table.
DEFAULT_LOCK_TIMEOUT = "10s"

target_metadata = None


def database_url() -> URL:
    arguments = context.get_x_argument(as_dictionary=True)
    raw = arguments.get("url") or os.environ.get("GEA_DATABASE_URL") or config.get_main_option("sqlalchemy.url")
    if not raw:
        raise SystemExit("Set GEA_DATABASE_URL, e.g. postgresql://gea_owner:secret@localhost:5432/gea")
    url = make_url(raw)
    if url.drivername in ("postgresql", "postgres"):
        url = url.set(drivername="postgresql+psycopg")       # psycopg 3
    return url


def configure(**options) -> None:
    context.configure(
        target_metadata=target_metadata,
        version_table=VERSION_TABLE,
        version_table_schema=SCHEMA,
        # PostgreSQL DDL is transactional: an upgrade over several revisions
        # is applied completely or not at all.
        transaction_per_migration=False,
        **options,
    )


def run_migrations_offline() -> None:
    """Write the SQL to stdout (`--sql`); nothing connects to a database."""
    configure(url=database_url().render_as_string(hide_password=False), literal_binds=True,
              dialect_opts={"paramstyle": "named"})
    with context.begin_transaction():
        context.execute(f"CREATE SCHEMA IF NOT EXISTS {SCHEMA}")
        context.run_migrations()


def run_migrations_online() -> None:
    lock_timeout = context.get_x_argument(as_dictionary=True).get("lock_timeout", DEFAULT_LOCK_TIMEOUT)
    engine = create_engine(database_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        connection.execute(text("SELECT pg_advisory_lock(:key)"), {"key": LOCK_KEY})
        connection.execute(text("SELECT set_config('lock_timeout', :value, false)"), {"value": lock_timeout})
        # gea."AlembicVersion" lives in the schema, so the schema must exist before
        # Alembic looks for its version table. Checked first because CREATE SCHEMA
        # IF NOT EXISTS needs the CREATE privilege on the database even when the
        # schema is already there.
        exists = connection.execute(text("SELECT 1 FROM pg_namespace WHERE nspname = :name"), {"name": SCHEMA})
        if exists.scalar() is None:
            connection.execute(text(f"CREATE SCHEMA {SCHEMA}"))
        connection.commit()

        configure(connection=connection)
        with context.begin_transaction():
            context.run_migrations()
    # Closing the connection released the session-level advisory lock.


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
