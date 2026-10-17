"""Configurable credits: task costs, packages, quotes, purchases and ledger.

Revision ID: y1z2a3b4c5d6
Revises: x0y1z2a3b4c5

No prices, costs or plan credits are inserted: everything stays empty/null
until configured in the admin panel.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "y1z2a3b4c5d6"
down_revision = "x0y1z2a3b4c5"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def _timestamps():
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    ]


def upgrade():
    op.add_column("plan_configs", sa.Column("monthly_credits", sa.Integer, nullable=True))
    op.create_table(
        "credit_task_costs",
        sa.Column("task_key", sa.String(40), primary_key=True),
        sa.Column("credits", sa.Integer, nullable=False),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("updated_by", sa.String(255), nullable=True),
        *_timestamps(),
    )
    op.create_table(
        "credit_packages",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("label", sa.String(80), nullable=False),
        sa.Column("credits", sa.Integer, nullable=False),
        sa.Column("price_brl", sa.Numeric(10, 2), nullable=False),
        sa.Column("validity_days", sa.Integer, nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default="false"),
        sa.Column("sort_order", sa.Integer, nullable=False, server_default="0"),
        sa.Column("updated_by", sa.String(255), nullable=True),
        *_timestamps(),
    )
    op.create_table(
        "credit_quotes",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("task_key", sa.String(40), nullable=False),
        sa.Column("cost", sa.Integer, nullable=False),
        sa.Column("scope", sa.JSON, nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="quoted"),
        sa.Column("task_ref", sa.String(100), nullable=True),
        sa.Column("failure_reason", sa.String(200), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("settled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_credit_quotes_user_id", "credit_quotes", ["user_id"])
    op.create_table(
        "credit_purchases",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("package_id", UUID, sa.ForeignKey("credit_packages.id", ondelete="SET NULL"), nullable=True),
        sa.Column("label", sa.String(80), nullable=False),
        sa.Column("credits", sa.Integer, nullable=False),
        sa.Column("price_brl", sa.Numeric(10, 2), nullable=False),
        sa.Column("validity_days", sa.Integer, nullable=True),
        sa.Column("provider", sa.String(30), nullable=False),
        sa.Column("provider_ref", sa.String(200), nullable=True),
        sa.Column("checkout_url", sa.String(1000), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_credit_purchases_user_id", "credit_purchases", ["user_id"])
    op.create_index("ix_credit_purchases_provider_ref", "credit_purchases", ["provider_ref"])
    op.create_table(
        "credit_ledger",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(20), nullable=False),
        sa.Column("bucket", sa.String(10), nullable=False),
        sa.Column("amount", sa.Integer, nullable=False),
        sa.Column("lot_id", UUID, sa.ForeignKey("credit_ledger.id", ondelete="CASCADE"), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("period_start", sa.Date, nullable=True),
        sa.Column("quote_id", UUID, sa.ForeignKey("credit_quotes.id", ondelete="SET NULL"), nullable=True),
        sa.Column("purchase_id", UUID, sa.ForeignKey("credit_purchases.id", ondelete="SET NULL"), nullable=True),
        sa.Column("task_key", sa.String(40), nullable=True),
        sa.Column("task_ref", sa.String(100), nullable=True),
        sa.Column("description", sa.String(200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.UniqueConstraint("user_id", "period_start", name="uq_credit_ledger_plan_period"),
    )
    op.create_index("ix_credit_ledger_user_id", "credit_ledger", ["user_id"])
    op.create_index("ix_credit_ledger_lot_id", "credit_ledger", ["lot_id"])
    op.create_index("ix_credit_ledger_quote_id", "credit_ledger", ["quote_id"])


def downgrade():
    op.drop_table("credit_ledger")
    op.drop_table("credit_purchases")
    op.drop_table("credit_quotes")
    op.drop_table("credit_packages")
    op.drop_table("credit_task_costs")
    op.drop_column("plan_configs", "monthly_credits")
