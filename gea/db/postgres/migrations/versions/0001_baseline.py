"""baseline: lookups, users, idempotency receipts and the audit log

Revision ID: 0001
Revises: 
Create Date: 2026-10-02
"""
from gea_sql import run_sql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql(__file__, "up")


def downgrade() -> None:
    run_sql(__file__, "down")
