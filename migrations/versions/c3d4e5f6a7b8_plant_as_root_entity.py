"""plant as root entity — add user_id/grow_label/pot fields to plants, swap sensor_devices.grow_id for plant_id

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-06 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # plants — novos campos
    # ------------------------------------------------------------------
    op.add_column(
        'plants',
        sa.Column('user_id', UUID(as_uuid=True), nullable=True)  # temporariamente nullable para backfill
    )
    op.add_column(
        'plants',
        sa.Column('grow_label', sa.String(length=100), nullable=True)
    )
    op.add_column(
        'plants',
        sa.Column('pot_label', sa.String(length=100), nullable=True)
    )
    op.add_column(
        'plants',
        sa.Column('pot_volume_liters', sa.Float(), nullable=True)
    )
    op.add_column(
        'plants',
        sa.Column('substrate', sa.String(length=100), nullable=True)
    )

    # Backfill user_id e grow_label a partir da hierarquia Grow → Pot → Plant
    op.execute("""
        UPDATE plants p
        SET
            user_id        = g.user_id,
            grow_label     = g.name,
            pot_label      = po.label,
            pot_volume_liters = po.volume_liters,
            substrate      = po.substrate
        FROM pots po
        JOIN grows g ON g.id = po.grow_id
        WHERE po.id = p.pot_id
    """)

    # Agora que todos os registros têm user_id, tornar NOT NULL e criar FK + index
    op.alter_column('plants', 'user_id', nullable=False)
    op.create_foreign_key(
        'fk_plants_user_id',
        'plants', 'users',
        ['user_id'], ['id'],
        ondelete='CASCADE'
    )
    op.create_index('ix_plants_user_id', 'plants', ['user_id'])
    op.create_index('ix_plants_grow_label', 'plants', ['grow_label'])

    # ------------------------------------------------------------------
    # sensor_devices — trocar grow_id + pot_id por plant_id
    # ------------------------------------------------------------------
    op.add_column(
        'sensor_devices',
        sa.Column('plant_id', UUID(as_uuid=True), nullable=True)
    )

    # Backfill plant_id via pot_id (pots → plants via pot_id)
    op.execute("""
        UPDATE sensor_devices sd
        SET plant_id = pl.id
        FROM plants pl
        WHERE pl.pot_id = sd.pot_id
          AND sd.pot_id IS NOT NULL
    """)

    op.create_foreign_key(
        'fk_sensor_devices_plant_id',
        'sensor_devices', 'plants',
        ['plant_id'], ['id'],
        ondelete='SET NULL'
    )
    op.create_index('ix_sensor_devices_plant_id', 'sensor_devices', ['plant_id'])

    # Remover FK, index e colunas antigas de sensor_devices
    # (grow_id tinha FK para grows; pot_id tinha FK para pots)
    op.drop_index(op.f('ix_sensor_devices_grow_id'), table_name='sensor_devices')
    op.drop_constraint('sensor_devices_grow_id_fkey', 'sensor_devices', type_='foreignkey')
    op.drop_constraint('sensor_devices_pot_id_fkey', 'sensor_devices', type_='foreignkey')
    op.drop_column('sensor_devices', 'grow_id')
    op.drop_column('sensor_devices', 'pot_id')

    # NOTA: as tabelas grows e pots são preservadas intencionalmente
    # para não perder dados históricos. Serão removidas em migration futura.


def downgrade() -> None:
    # ------------------------------------------------------------------
    # Restaurar sensor_devices
    # ------------------------------------------------------------------
    op.add_column(
        'sensor_devices',
        sa.Column('grow_id', UUID(as_uuid=True), nullable=True)
    )
    op.add_column(
        'sensor_devices',
        sa.Column('pot_id', UUID(as_uuid=True), nullable=True)
    )

    # Tentar recuperar grow_id/pot_id via plant → pot
    op.execute("""
        UPDATE sensor_devices sd
        SET
            pot_id  = pl.pot_id,
            grow_id = po.grow_id
        FROM plants pl
        JOIN pots po ON po.id = pl.pot_id
        WHERE pl.id = sd.plant_id
          AND sd.plant_id IS NOT NULL
    """)

    op.create_foreign_key(
        'sensor_devices_grow_id_fkey',
        'sensor_devices', 'grows',
        ['grow_id'], ['id'],
        ondelete='CASCADE'
    )
    op.create_foreign_key(
        'sensor_devices_pot_id_fkey',
        'sensor_devices', 'pots',
        ['pot_id'], ['id'],
        ondelete='SET NULL'
    )

    op.drop_index('ix_sensor_devices_plant_id', table_name='sensor_devices')
    op.drop_constraint('fk_sensor_devices_plant_id', 'sensor_devices', type_='foreignkey')
    op.drop_column('sensor_devices', 'plant_id')

    # ------------------------------------------------------------------
    # Reverter plants
    # ------------------------------------------------------------------
    op.drop_constraint('fk_plants_user_id', 'plants', type_='foreignkey')
    op.drop_index('ix_plants_user_id', table_name='plants')
    op.drop_index('ix_plants_grow_label', table_name='plants')
    op.drop_column('plants', 'user_id')
    op.drop_column('plants', 'grow_label')
    op.drop_column('plants', 'pot_label')
    op.drop_column('plants', 'pot_volume_liters')
    op.drop_column('plants', 'substrate')
