"""Manual environment readings that belong to a grow, without a plant.

Revision ID: v8w9x0y1z2a3
Revises: u7v8w9x0y1z2
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "v8w9x0y1z2a3"
down_revision = "u7v8w9x0y1z2"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "environment_readings",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("grow_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("grows.id", ondelete="CASCADE"), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("temperature_c", sa.Float, nullable=True),
        sa.Column("humidity_rh", sa.Float, nullable=True),
        sa.Column("notes", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("ix_environment_readings_grow_observed", "environment_readings", ["grow_id", "observed_at"])
    op.create_index("ix_environment_readings_user_id", "environment_readings", ["user_id"])


def downgrade():
    op.drop_index("ix_environment_readings_user_id", table_name="environment_readings")
    op.drop_index("ix_environment_readings_grow_observed", table_name="environment_readings")
    op.drop_table("environment_readings")
