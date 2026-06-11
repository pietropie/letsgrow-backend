"""plan_configs table

Revision ID: i4j5k6l7m8n9
Revises: h3i4j5k6l7m8
Create Date: 2026-06-11
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "i4j5k6l7m8n9"
down_revision = "h3i4j5k6l7m8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "plan_configs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("plan_key", sa.String(30), unique=True, nullable=False),
        sa.Column("label", sa.String(50), nullable=False),
        sa.Column("price_brl", sa.Numeric(8, 2), nullable=False, server_default="0"),
        sa.Column("price_display", sa.String(30), nullable=False, server_default="R$ 0"),
        sa.Column("period_display", sa.String(30), nullable=False, server_default="para sempre"),
        sa.Column("badge_text", sa.String(50), nullable=True),
        sa.Column("max_plants", sa.Integer, nullable=False, server_default="1"),
        sa.Column("max_grows", sa.Integer, nullable=False, server_default="1"),
        sa.Column("max_pots_per_grow", sa.Integer, nullable=False, server_default="3"),
        sa.Column("ai_queries_per_month", sa.Integer, nullable=True),
        sa.Column("sensors_allowed", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()")),
    )


def downgrade() -> None:
    op.drop_table("plan_configs")
