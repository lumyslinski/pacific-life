"""data contract, delivery to Snowflake, execution status and read models

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-02
"""
from gea_sql import run_sql

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql(__file__, "up")


def downgrade() -> None:
    run_sql(__file__, "down")
