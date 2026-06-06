"""add mvp fields to grow_events

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-05 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Environmental / physical measurements
    op.add_column('grow_events', sa.Column('temperature_c', sa.Float(), nullable=True))
    op.add_column('grow_events', sa.Column('humidity_rh', sa.Float(), nullable=True))
    op.add_column('grow_events', sa.Column('weight_g', sa.Float(), nullable=True))
    # Problem severity — accepted values: "leve", "moderado", "grave"
    op.add_column('grow_events', sa.Column('severity', sa.String(length=10), nullable=True))
    # Watering — True when run used plain water only (no nutrients)
    op.add_column('grow_events', sa.Column('is_flush', sa.Boolean(), nullable=True))


def downgrade() -> None:
    op.drop_column('grow_events', 'is_flush')
    op.drop_column('grow_events', 'severity')
    op.drop_column('grow_events', 'weight_g')
    op.drop_column('grow_events', 'humidity_rh')
    op.drop_column('grow_events', 'temperature_c')
