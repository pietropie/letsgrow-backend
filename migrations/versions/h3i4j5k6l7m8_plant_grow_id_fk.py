"""plant_grow_id_fk

Adiciona grow_id (UUID, nullable, FK → grows.id SET NULL) à tabela plants.
Permite vincular opcionalmente uma planta a um grow registrado.
grow_label continua como fallback de texto livre.

Revision ID: h3i4j5k6l7m8
Revises: g2h3i4j5k6l7
Create Date: 2026-06-09 00:00:00.000000
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

revision = 'h3i4j5k6l7m8'
down_revision = 'g2h3i4j5k6l7'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        'plants',
        sa.Column('grow_id', UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        'fk_plants_grow_id',
        'plants', 'grows',
        ['grow_id'], ['id'],
        ondelete='SET NULL',
    )
    op.create_index('ix_plants_grow_id', 'plants', ['grow_id'])


def downgrade() -> None:
    op.drop_index('ix_plants_grow_id', table_name='plants')
    op.drop_constraint('fk_plants_grow_id', 'plants', type_='foreignkey')
    op.drop_column('plants', 'grow_id')
