"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}

SQL: sql/<this file's name>.up.sql and sql/<this file's name>.down.sql
"""
from gea_sql import run_sql

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    run_sql(__file__, "up")


def downgrade() -> None:
    run_sql(__file__, "down")
