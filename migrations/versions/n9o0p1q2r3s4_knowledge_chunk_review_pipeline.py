"""knowledge_chunk_review_pipeline

Adds status, confidence_score, source_conversation_id and extraction_reasoning
to knowledge_chunks — enables the admin review pipeline for AI-extracted RAG chunks.

Revision ID: n9o0p1q2r3s4
Revises: m8n9o0p1q2r3
Create Date: 2026-06-23
"""

from alembic import op
import sqlalchemy as sa

# revision identifiers
revision = "n9o0p1q2r3s4"
down_revision = "m8n9o0p1q2r3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # status: active (default, existing chunks) | draft | rejected
    op.add_column(
        "knowledge_chunks",
        sa.Column(
            "status",
            sa.String(20),
            nullable=False,
            server_default="active",
        ),
    )
    op.create_index("ix_knowledge_chunks_status", "knowledge_chunks", ["status"])

    # LLM confidence score for extracted chunks
    op.add_column(
        "knowledge_chunks",
        sa.Column("confidence_score", sa.Float, nullable=True),
    )

    # FK to the conversation this chunk was extracted from
    op.add_column(
        "knowledge_chunks",
        sa.Column(
            "source_conversation_id",
            sa.UUID(as_uuid=True),
            sa.ForeignKey("ai_conversations.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_knowledge_chunks_source_conversation_id",
        "knowledge_chunks",
        ["source_conversation_id"],
    )

    # LLM reasoning text for why this chunk is valuable
    op.add_column(
        "knowledge_chunks",
        sa.Column("extraction_reasoning", sa.Text, nullable=True),
    )


def downgrade() -> None:
    op.drop_index("ix_knowledge_chunks_source_conversation_id", table_name="knowledge_chunks")
    op.drop_index("ix_knowledge_chunks_status", table_name="knowledge_chunks")
    op.drop_column("knowledge_chunks", "extraction_reasoning")
    op.drop_column("knowledge_chunks", "source_conversation_id")
    op.drop_column("knowledge_chunks", "confidence_score")
    op.drop_column("knowledge_chunks", "status")
