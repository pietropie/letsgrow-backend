"""Usage warning threshold for GET /usage.

Revision ID: b0c1d2e3f4a5
Revises: a9b0c1d2e3f4

- app_settings gains usage_warning_threshold: the fraction of the monthly allowance
  from which GET /usage reports near_limit (the Home screen warns). Default 0.8.
"""
from alembic import op
import sqlalchemy as sa

revision = "b0c1d2e3f4a5"
down_revision = "a9b0c1d2e3f4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "app_settings",
        sa.Column("usage_warning_threshold", sa.Float(), nullable=False, server_default="0.8"),
    )


def downgrade() -> None:
    op.drop_column("app_settings", "usage_warning_threshold")
