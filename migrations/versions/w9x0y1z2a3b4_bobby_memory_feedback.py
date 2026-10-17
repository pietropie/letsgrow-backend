"""Bobby: user-provided memory and context feedback.

Revision ID: w9x0y1z2a3b4
Revises: v8w9x0y1z2a3
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "w9x0y1z2a3b4"
down_revision = "v8w9x0y1z2a3"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade():
    op.create_table(
        "bob_memories",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("content", sa.String(300), nullable=False),
        sa.Column("source", sa.String(20), nullable=False, server_default="user"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("ix_bob_memories_user_id", "bob_memories", ["user_id"])
    op.create_table(
        "bob_feedback",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("alert_id", sa.String(200), nullable=True),
        sa.Column("plant_id", UUID, sa.ForeignKey("plants.id", ondelete="SET NULL"), nullable=True),
        sa.Column("grow_id", UUID, sa.ForeignKey("grows.id", ondelete="SET NULL"), nullable=True),
        sa.Column("evidence_ids", sa.JSON, nullable=True),
        sa.Column("message", sa.Text, nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="new"),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("ix_bob_feedback_user_id", "bob_feedback", ["user_id"])


def downgrade():
    op.drop_index("ix_bob_feedback_user_id", table_name="bob_feedback")
    op.drop_table("bob_feedback")
    op.drop_index("ix_bob_memories_user_id", table_name="bob_memories")
    op.drop_table("bob_memories")
