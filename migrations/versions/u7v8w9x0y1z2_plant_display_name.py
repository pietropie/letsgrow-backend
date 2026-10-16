"""Separate plant identity from variety; preserve existing records.

Revision ID: u7v8w9x0y1z2
Revises: t6u7v8w9x0y1
"""
from alembic import op
import sqlalchemy as sa

revision = "u7v8w9x0y1z2"
down_revision = "t6u7v8w9x0y1"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("plants", sa.Column("display_name", sa.String(100), nullable=True))


def downgrade():
    op.drop_column("plants", "display_name")
