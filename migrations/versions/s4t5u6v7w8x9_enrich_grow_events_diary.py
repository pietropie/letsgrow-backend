"""enrich grow_events diary fields

Revision ID: s4t5u6v7w8x9
Revises: r3s4t5u6v7w8
Create Date: 2026-07-03

Adiciona campos para cobrir o redesign completo do diário:
  - Runoff: ec_out, has_runoff
  - Subtipos: training_subtype, nutrient_subtype
  - Tricomas: trichome_clear_pct, trichome_milky_pct, trichome_amber_pct
  - Catch-all: metadata (JSON)
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSON

revision = 's4t5u6v7w8x9'
down_revision = 'r3s4t5u6v7w8'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Runoff — EC/PPM de saída e flag se houve escoamento
    op.add_column('grow_events', sa.Column('ec_out', sa.Float(), nullable=True))
    op.add_column('grow_events', sa.Column('has_runoff', sa.Boolean(), nullable=True))

    # Subtipos — diferencia técnica de treinamento e tipo de nutrição
    op.add_column('grow_events', sa.Column('training_subtype', sa.String(30), nullable=True))
    op.add_column('grow_events', sa.Column('nutrient_subtype', sa.String(30), nullable=True))

    # Tricomas — % de cada estágio para decisão de colheita
    op.add_column('grow_events', sa.Column('trichome_clear_pct', sa.Integer(), nullable=True))
    op.add_column('grow_events', sa.Column('trichome_milky_pct', sa.Integer(), nullable=True))
    op.add_column('grow_events', sa.Column('trichome_amber_pct', sa.Integer(), nullable=True))

    # Metadata — catch-all para campos futuros (harvest_method, symptom_type, etc.)
    op.add_column('grow_events', sa.Column('metadata', JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column('grow_events', 'metadata')
    op.drop_column('grow_events', 'trichome_amber_pct')
    op.drop_column('grow_events', 'trichome_milky_pct')
    op.drop_column('grow_events', 'trichome_clear_pct')
    op.drop_column('grow_events', 'nutrient_subtype')
    op.drop_column('grow_events', 'training_subtype')
    op.drop_column('grow_events', 'has_runoff')
    op.drop_column('grow_events', 'ec_out')
