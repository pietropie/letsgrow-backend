"""grow_environment_fields

Adiciona campos de configuração de ambiente ao grow:
  - Iluminação: light_type, light_distance_cm, photoperiod_hours, light_leak_controlled
  - Ventilação: exhaust_type, carbon_filter, intake_type, internal_circulation_fans, negative_pressure
  - Clima: air_conditioning, dehumidifier, humidifier, heater
  - Substrato: pot_type, substrate_type
  - Sensores (extensibilidade futura): has_environment_sensors

Revision ID: g2h3i4j5k6l7
Revises: f1a2b3c4d5e6
Create Date: 2026-06-09 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = 'g2h3i4j5k6l7'
down_revision = 'f1a2b3c4d5e6'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # --- Iluminação ---
    op.add_column('grows', sa.Column('light_type', sa.String(30), nullable=True))
    # led_quantum_board | led_cob | hps_hid | cmh | fluorescent | other
    op.add_column('grows', sa.Column('light_distance_cm', sa.Integer(), nullable=True))
    op.add_column('grows', sa.Column('photoperiod_hours', sa.String(10), nullable=True))
    # "18/6" | "12/12" | "20/4" | "24/0" | "custom"
    op.add_column('grows', sa.Column('light_leak_controlled', sa.Boolean(), nullable=True))

    # --- Ventilação ---
    op.add_column('grows', sa.Column('exhaust_type', sa.String(20), nullable=True))
    # inline_fan | axial_fan | pc_fans | none
    op.add_column('grows', sa.Column('carbon_filter', sa.Boolean(), nullable=True))
    op.add_column('grows', sa.Column('intake_type', sa.String(20), nullable=True))
    # active_fan | passive | none
    op.add_column('grows', sa.Column('internal_circulation_fans', sa.Integer(), nullable=True))
    op.add_column('grows', sa.Column('negative_pressure', sa.Boolean(), nullable=True))

    # --- Controle climático ---
    op.add_column('grows', sa.Column('air_conditioning', sa.Boolean(), nullable=True))
    op.add_column('grows', sa.Column('dehumidifier', sa.Boolean(), nullable=True))
    op.add_column('grows', sa.Column('humidifier', sa.Boolean(), nullable=True))
    op.add_column('grows', sa.Column('heater', sa.Boolean(), nullable=True))

    # --- Substrato ---
    op.add_column('grows', sa.Column('pot_type', sa.String(20), nullable=True))
    # fabric | plastic | air_pot | other
    op.add_column('grows', sa.Column('substrate_type', sa.String(30), nullable=True))
    # mineral_soil | coco | organic_supersoil | hydro_dwc | other

    # --- Extensibilidade: sensores ---
    op.add_column('grows', sa.Column('has_environment_sensors', sa.Boolean(), nullable=True, server_default='false'))


def downgrade() -> None:
    op.drop_column('grows', 'has_environment_sensors')
    op.drop_column('grows', 'substrate_type')
    op.drop_column('grows', 'pot_type')
    op.drop_column('grows', 'heater')
    op.drop_column('grows', 'humidifier')
    op.drop_column('grows', 'dehumidifier')
    op.drop_column('grows', 'air_conditioning')
    op.drop_column('grows', 'negative_pressure')
    op.drop_column('grows', 'internal_circulation_fans')
    op.drop_column('grows', 'intake_type')
    op.drop_column('grows', 'carbon_filter')
    op.drop_column('grows', 'exhaust_type')
    op.drop_column('grows', 'light_leak_controlled')
    op.drop_column('grows', 'photoperiod_hours')
    op.drop_column('grows', 'light_distance_cm')
    op.drop_column('grows', 'light_type')
