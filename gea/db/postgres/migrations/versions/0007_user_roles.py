"""user roles: Viewer, Preparer, Reviewer and Admin, referenced from User

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-02
"""
from gea_sql import run_sql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql(__file__, "up")


def downgrade() -> None:
    run_sql(__file__, "down")
