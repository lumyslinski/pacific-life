"""jobs, failure resolution, cancel, execution log and delivery in job order

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-02
"""
from gea_sql import run_sql

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql(__file__, "up")


def downgrade() -> None:
    run_sql(__file__, "down")
