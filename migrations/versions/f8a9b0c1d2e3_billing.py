"""Billing: account subscriptions, plan prices per period, provider events, Play product ids.

Revision ID: f8a9b0c1d2e3
Revises: e7f8a9b0c1d2

- plan_prices starts empty: nothing is for sale until the admin sets a price and activates it.
- credit_purchases.provider_ref grows to 500 (Google Play purchase tokens).
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "f8a9b0c1d2e3"
down_revision = "e7f8a9b0c1d2"
branch_labels = None
depends_on = None

UUID = postgresql.UUID(as_uuid=True)


def upgrade():
    op.create_table(
        "plan_prices",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("plan_key", sa.String(30), nullable=False),
        sa.Column("period", sa.String(10), nullable=False),
        sa.Column("price_brl", sa.Numeric(10, 2), nullable=True),
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("google_product_id", sa.String(100), nullable=True),
        sa.Column("google_base_plan_id", sa.String(100), nullable=True),
        sa.Column("updated_by", sa.String(255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.UniqueConstraint("plan_key", "period", name="uq_plan_prices_plan_period"),
    )
    op.create_table(
        "subscriptions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("user_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True),
        sa.Column("plan_key", sa.String(30), nullable=False),
        sa.Column("period", sa.String(10), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False, server_default="recurring"),
        sa.Column("provider_ref", sa.String(500), nullable=True),
        sa.Column("checkout_url", sa.String(1000), nullable=True),
        sa.Column("price_brl", sa.Numeric(10, 2), nullable=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("auto_renew", sa.Boolean, nullable=False, server_default=sa.false()),
        sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
        sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
        sa.Column("canceled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_payment_ref", sa.String(100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
    )
    op.create_index("ix_subscriptions_user_id", "subscriptions", ["user_id"])
    op.create_index("ix_subscriptions_provider_ref", "subscriptions", ["provider_ref"])
    op.create_table(
        "payment_events",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("event_id", sa.String(200), nullable=False),
        sa.Column("kind", sa.String(60), nullable=False),
        sa.Column("payload", sa.JSON, nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("provider", "event_id", name="uq_payment_events_provider_event"),
    )
    op.add_column("credit_packages", sa.Column("google_product_id", sa.String(100), nullable=True))
    op.create_unique_constraint("uq_credit_packages_google_product_id", "credit_packages", ["google_product_id"])
    op.alter_column("credit_purchases", "provider_ref", type_=sa.String(500), existing_type=sa.String(200),
                    existing_nullable=True)


def downgrade():
    op.alter_column("credit_purchases", "provider_ref", type_=sa.String(200), existing_type=sa.String(500),
                    existing_nullable=True)
    op.drop_constraint("uq_credit_packages_google_product_id", "credit_packages", type_="unique")
    op.drop_column("credit_packages", "google_product_id")
    op.drop_table("payment_events")
    op.drop_index("ix_subscriptions_provider_ref", table_name="subscriptions")
    op.drop_index("ix_subscriptions_user_id", table_name="subscriptions")
    op.drop_table("subscriptions")
    op.drop_table("plan_prices")
