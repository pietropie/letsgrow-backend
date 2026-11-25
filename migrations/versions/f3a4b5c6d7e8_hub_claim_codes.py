"""Códigos de vínculo do Hub (onboarding por BLE).

Revision ID: f3a4b5c6d7e8
Revises: e3f4a5b6c7d8
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "f3a4b5c6d7e8"
down_revision = "e3f4a5b6c7d8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "hub_claim_codes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("code_hash", sa.String(64), nullable=False),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "grow_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("grows.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("used_by_mac", sa.String(12), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("code_hash", name="uq_hub_claim_codes_code_hash"),
    )
    op.create_index("ix_hub_claim_codes_user_id", "hub_claim_codes", ["user_id"])
    op.create_index("ix_hub_claim_codes_grow_id", "hub_claim_codes", ["grow_id"])


def downgrade() -> None:
    op.drop_index("ix_hub_claim_codes_grow_id", table_name="hub_claim_codes")
    op.drop_index("ix_hub_claim_codes_user_id", table_name="hub_claim_codes")
    op.drop_table("hub_claim_codes")
