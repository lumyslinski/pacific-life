"""calculation audit: append-only copy of the calculation module's audit events

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-02
"""
from gea_sql import run_sql

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql(__file__, "up")


def downgrade() -> None:
    run_sql(__file__, "down")
