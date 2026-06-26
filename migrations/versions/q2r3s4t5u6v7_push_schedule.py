"""push_schedule — adiciona colunas de agendamento de push na tabela ai_config

Revision ID: q2r3s4t5u6v7
Revises: p1q2r3s4t5u6
Create Date: 2026-06-26
"""
from alembic import op
import sqlalchemy as sa

revision = "q2r3s4t5u6v7"
down_revision = "p1q2r3s4t5u6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("ai_config", sa.Column("daily_push_enabled", sa.Boolean(), nullable=False, server_default="true"))
    op.add_column("ai_config", sa.Column("daily_push_hour", sa.Integer(), nullable=False, server_default="9"))
    op.add_column("ai_config", sa.Column("daily_push_minute", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("ai_config", sa.Column("daily_push_last_run_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("ai_config", sa.Column("daily_push_last_stats", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("ai_config", "daily_push_last_stats")
    op.drop_column("ai_config", "daily_push_last_run_at")
    op.drop_column("ai_config", "daily_push_minute")
    op.drop_column("ai_config", "daily_push_hour")
    op.drop_column("ai_config", "daily_push_enabled")
