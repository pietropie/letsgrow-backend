"""Drop the "Popular" badge from the Cultivador plan.

Revision ID: c1d2e3f4a5b6
Revises: b0c1d2e3f4a5

- Social proof only with real data (UX item 19): the Cultivador badge_text
  "Popular" was invented. Cleared only while it still holds the seed value, so
  a text the admin wrote by hand is kept. The suggested plan now comes from
  recommended_plan_key in GET /billing/catalog.
"""
from alembic import op

revision = "c1d2e3f4a5b6"
down_revision = "b0c1d2e3f4a5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE plan_configs SET badge_text = NULL "
        "WHERE plan_key = 'cultivador' AND badge_text = 'Popular'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE plan_configs SET badge_text = 'Popular' "
        "WHERE plan_key = 'cultivador' AND badge_text IS NULL"
    )
