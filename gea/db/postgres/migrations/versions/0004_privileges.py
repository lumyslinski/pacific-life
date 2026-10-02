"""privileges: grants for the roles gea_app and gea_relay

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-02
"""
from gea_sql import run_sql

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql(__file__, "up")


def downgrade() -> None:
    run_sql(__file__, "down")
