"""ai_config: add injection_message column

Revision ID: o0p1q2r3s4t5
Revises: n9o0p1q2r3s4
Create Date: 2026-06-23 00:00:00.000000
"""

from alembic import op
import sqlalchemy as sa

revision = "o0p1q2r3s4t5"
down_revision = "n9o0p1q2r3s4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_config",
        sa.Column("injection_message", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("ai_config", "injection_message")
