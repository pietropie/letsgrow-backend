"""Archive a grow together with its plants; kind of environment reading.

Revision ID: b4c5d6e7f8a9
Revises: a3b4c5d6e7f8

- plants.archived_with_grow_id: set when the plant was archived by archiving its grow,
  so restoring the grow brings back only those plants.
- grows.archived_at: when the grow was archived (null while active).
- environment_readings.kind: 'manual' (typed in the app) or 'exhaust_power' (automatic
  record of the exhaust dimmer). Existing exhaust records are backfilled by their note.
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "b4c5d6e7f8a9"
down_revision = "a3b4c5d6e7f8"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("plants", sa.Column(
        "archived_with_grow_id", postgresql.UUID(as_uuid=True),
        sa.ForeignKey("grows.id", ondelete="SET NULL"), nullable=True,
    ))
    op.create_index("ix_plants_archived_with_grow_id", "plants", ["archived_with_grow_id"])
    op.add_column("grows", sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("environment_readings", sa.Column(
        "kind", sa.String(20), nullable=False, server_default="manual",
    ))
    op.create_check_constraint(
        "ck_environment_readings_kind", "environment_readings", "kind IN ('manual', 'exhaust_power')",
    )
    op.execute(
        "UPDATE environment_readings SET kind = 'exhaust_power' "
        "WHERE notes LIKE 'Potência do exaustor %' AND temperature_c IS NULL AND humidity_rh IS NULL"
    )


def downgrade():
    op.drop_constraint("ck_environment_readings_kind", "environment_readings", type_="check")
    op.drop_column("environment_readings", "kind")
    op.drop_column("grows", "archived_at")
    op.drop_index("ix_plants_archived_with_grow_id", table_name="plants")
    op.drop_column("plants", "archived_with_grow_id")
