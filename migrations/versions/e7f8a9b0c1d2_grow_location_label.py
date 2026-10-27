"""Grow location for the Home weather card: readable label and coarse coordinates.

Revision ID: e7f8a9b0c1d2
Revises: d6e7f8a9b0c1

- grows gains location_label (e.g. "Campinas, SP").
- lat/lon already stored are rounded to 2 decimals (~1 km), the precision the API now keeps.
  Values outside the valid range, or with only one of the pair, are cleared.
  The rounding is not undone on downgrade.
"""
from alembic import op
import sqlalchemy as sa

revision = "e7f8a9b0c1d2"
down_revision = "d6e7f8a9b0c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("grows", sa.Column("location_label", sa.String(length=120), nullable=True))
    op.execute(
        "UPDATE grows SET lat = NULL, lon = NULL "
        "WHERE (lat IS NULL) <> (lon IS NULL) OR lat NOT BETWEEN -90 AND 90 OR lon NOT BETWEEN -180 AND 180"
    )
    op.execute(
        "UPDATE grows SET lat = ROUND(CAST(lat AS NUMERIC), 2), lon = ROUND(CAST(lon AS NUMERIC), 2) "
        "WHERE lat IS NOT NULL AND lon IS NOT NULL"
    )


def downgrade() -> None:
    op.drop_column("grows", "location_label")
