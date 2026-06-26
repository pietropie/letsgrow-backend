"""user push token + daily brief sent at

Revision ID: p1q2r3s4t5u6
Revises: o0p1q2r3s4t5
Create Date: 2026-06-26
"""
from alembic import op
import sqlalchemy as sa

revision = "p1q2r3s4t5u6"
down_revision = "o0p1q2r3s4t5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("expo_push_token", sa.String(length=200), nullable=True))
    op.add_column("users", sa.Column("daily_brief_sent_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "daily_brief_sent_at")
    op.drop_column("users", "expo_push_token")
