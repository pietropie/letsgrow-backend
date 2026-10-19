"""Grow: recorded exhaust power (record only, not an equipment command).

Revision ID: a3b4c5d6e7f8
Revises: z2a3b4c5d6e7
"""
from alembic import op
import sqlalchemy as sa

revision = "a3b4c5d6e7f8"
down_revision = "z2a3b4c5d6e7"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("grows", sa.Column("exhaust_power_pct", sa.Integer, nullable=True))
    op.add_column("grows", sa.Column("exhaust_power_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.create_check_constraint(
        "ck_grows_exhaust_power_pct",
        "grows",
        "exhaust_power_pct IS NULL OR (exhaust_power_pct BETWEEN 0 AND 100 AND exhaust_power_pct % 5 = 0)",
    )


def downgrade():
    op.drop_constraint("ck_grows_exhaust_power_pct", "grows", type_="check")
    op.drop_column("grows", "exhaust_power_updated_at")
    op.drop_column("grows", "exhaust_power_pct")
