"""project and run: one column per prop of workbook sheet POC Data

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-02
"""
from gea_sql import run_sql

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    run_sql(__file__, "up")


def downgrade() -> None:
    run_sql(__file__, "down")
