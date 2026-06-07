"""add strains table — catalog indexed from Brain/wiki/strains for autocomplete & info card

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-07 00:00:00.000001

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSON, UUID

revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'strains',
        sa.Column('id', UUID(as_uuid=True), primary_key=True),
        sa.Column('name', sa.String(length=150), nullable=False),
        sa.Column('slug', sa.String(length=170), nullable=False),
        sa.Column('aliases', JSON(), nullable=True),
        sa.Column('strain_type', sa.String(length=20), nullable=True),
        sa.Column('genetics', sa.String(length=20), nullable=True),
        sa.Column('breeder', sa.String(length=150), nullable=True),
        sa.Column('thc_pct', sa.String(length=30), nullable=True),
        sa.Column('cbd_pct', sa.String(length=30), nullable=True),
        sa.Column('dominant_terpene', sa.String(length=60), nullable=True),
        sa.Column('flowering_days', sa.Integer(), nullable=True),
        sa.Column('height_cm', sa.String(length=30), nullable=True),
        sa.Column('summary', sa.Text(), nullable=True),
        sa.Column('source_file', sa.String(length=255), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
    )
    op.create_index('ix_strains_name', 'strains', ['name'])
    op.create_index('ix_strains_slug', 'strains', ['slug'], unique=True)


def downgrade() -> None:
    op.drop_index('ix_strains_slug', table_name='strains')
    op.drop_index('ix_strains_name', table_name='strains')
    op.drop_table('strains')
