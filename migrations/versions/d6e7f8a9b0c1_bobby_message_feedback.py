"""Bobby chat: feedback per message and stable ids for stored messages.

Revision ID: d6e7f8a9b0c1
Revises: c5d6e7f8a9b0

- bob_feedback gains conversation_id, message_id, rating (up/down) and reason; message becomes
  optional (a thumbs up has no text).
- Every message already stored in ai_conversations.messages (JSON) gets an "id", so answers
  written before this version can also receive feedback. Existing ids are kept.
"""
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "d6e7f8a9b0c1"
down_revision = "c5d6e7f8a9b0"
branch_labels = None
depends_on = None


def backfill_message_ids(conn) -> int:
    conversations = sa.table("ai_conversations", sa.column("id"), sa.column("messages", sa.JSON))
    changed = 0
    for conversation_id, messages in conn.execute(sa.select(conversations.c.id, conversations.c.messages)).all():
        if not messages:
            continue
        updated = False
        for message in messages:
            if isinstance(message, dict) and not message.get("id"):
                message["id"] = str(uuid.uuid4())
                updated = True
        if updated:
            conn.execute(conversations.update().where(conversations.c.id == conversation_id).values(messages=messages))
            changed += 1
    return changed


def upgrade():
    op.add_column("bob_feedback", sa.Column(
        "conversation_id", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("ai_conversations.id", ondelete="CASCADE"), nullable=True,
    ))
    op.create_index("ix_bob_feedback_conversation_id", "bob_feedback", ["conversation_id"])
    op.add_column("bob_feedback", sa.Column("message_id", sa.String(64), nullable=True))
    op.add_column("bob_feedback", sa.Column("rating", sa.String(10), nullable=True))
    op.add_column("bob_feedback", sa.Column("reason", sa.String(30), nullable=True))
    op.alter_column("bob_feedback", "message", existing_type=sa.Text, nullable=True)
    op.create_check_constraint("ck_bob_feedback_rating", "bob_feedback", "rating IS NULL OR rating IN ('up', 'down')")
    op.create_check_constraint(
        "ck_bob_feedback_reason", "bob_feedback",
        "reason IS NULL OR reason IN ('wrong_record', 'missing_context', 'not_helpful', 'other')",
    )
    if not op.get_context().as_sql:
        backfill_message_ids(op.get_bind())


def downgrade():
    op.drop_constraint("ck_bob_feedback_reason", "bob_feedback", type_="check")
    op.drop_constraint("ck_bob_feedback_rating", "bob_feedback", type_="check")
    op.execute("UPDATE bob_feedback SET message = '' WHERE message IS NULL")
    op.alter_column("bob_feedback", "message", existing_type=sa.Text, nullable=False)
    op.drop_column("bob_feedback", "reason")
    op.drop_column("bob_feedback", "rating")
    op.drop_column("bob_feedback", "message_id")
    op.drop_index("ix_bob_feedback_conversation_id", table_name="bob_feedback")
    op.drop_column("bob_feedback", "conversation_id")
