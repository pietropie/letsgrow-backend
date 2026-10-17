"""Per-user notification preferences and admin-editable business settings.

Revision ID: x0y1z2a3b4c5
Revises: w9x0y1z2a3b4

The app_settings row is created with neutral defaults: credits off, 7-day
account deletion grace, empty support inbox, full notification preview
(the current push behaviour) and daily frequency.
"""
import uuid

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "x0y1z2a3b4c5"
down_revision = "w9x0y1z2a3b4"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade():
    op.create_table(
        "notification_preferences",
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("frequency", sa.String(10), nullable=False),
        sa.Column("brief_time", sa.Time, nullable=False),
        sa.Column("timezone", sa.String(64), nullable=False),
        sa.Column("quiet_start", sa.Time, nullable=True),
        sa.Column("quiet_end", sa.Time, nullable=True),
        sa.Column("preview", sa.String(10), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )
    settings_table = op.create_table(
        "app_settings",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("credits_enabled", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("credits_replace_query_limit", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("plan_credits_rollover", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("account_deletion_grace_days", sa.Integer, nullable=False, server_default="7"),
        sa.Column("support_inbox_email", sa.String(255), nullable=True),
        sa.Column("notification_default_preview", sa.String(10), nullable=False, server_default="full"),
        sa.Column("notification_default_frequency", sa.String(10), nullable=False, server_default="daily"),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )
    op.bulk_insert(settings_table, [{"id": uuid.uuid4()}])


def downgrade():
    op.drop_table("app_settings")
    op.drop_table("notification_preferences")
