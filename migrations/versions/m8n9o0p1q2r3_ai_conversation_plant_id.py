"""ai_conversation_plant_id

Adiciona coluna plant_id (FK opcional) à tabela ai_conversations.
Permite ao Bob rastrear qual planta estava em foco quando a conversa
foi iniciada, exibido como chip de contexto no histórico do mobile.

Revision ID: m8n9o0p1q2r3
Revises: l7m8n9o0p1q2
Create Date: 2026-06-16
"""
from alembic import op
import sqlalchemy as sa

revision = "m8n9o0p1q2r3"
down_revision = "l7m8n9o0p1q2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "ai_conversations",
        sa.Column(
            "plant_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("plants.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_ai_conversations_plant_id", "ai_conversations", ["plant_id"])


def downgrade() -> None:
    op.drop_index("ix_ai_conversations_plant_id", table_name="ai_conversations")
    op.drop_column("ai_conversations", "plant_id")
