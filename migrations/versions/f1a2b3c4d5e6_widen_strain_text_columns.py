"""widen strains.thc_pct/cbd_pct/height_cm from varchar(30) to varchar(120)

Revision ID: f1a2b3c4d5e6
Revises: e5f6a7b8c9d0
Create Date: 2026-06-07 19:00:00.000001

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = 'f1a2b3c4d5e6'
down_revision: Union[str, None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # O template de wiki/strains/ permite valores descritivos/qualificados
    # nesses campos (ex.: "~18% (variações relatadas até a faixa alta)",
    # "desconhecido (não confirmado na consulta à Leafly)"), que excedem os
    # 30 caracteres originalmente previstos. 120 chars cobre confortavelmente
    # esses casos sem virar um campo de texto livre.
    op.alter_column('strains', 'thc_pct',
                    existing_type=sa.String(length=30),
                    type_=sa.String(length=120),
                    existing_nullable=True)
    op.alter_column('strains', 'cbd_pct',
                    existing_type=sa.String(length=30),
                    type_=sa.String(length=120),
                    existing_nullable=True)
    op.alter_column('strains', 'height_cm',
                    existing_type=sa.String(length=30),
                    type_=sa.String(length=120),
                    existing_nullable=True)


def downgrade() -> None:
    op.alter_column('strains', 'height_cm',
                    existing_type=sa.String(length=120),
                    type_=sa.String(length=30),
                    existing_nullable=True)
    op.alter_column('strains', 'cbd_pct',
                    existing_type=sa.String(length=120),
                    type_=sa.String(length=30),
                    existing_nullable=True)
    op.alter_column('strains', 'thc_pct',
                    existing_type=sa.String(length=120),
                    type_=sa.String(length=30),
                    existing_nullable=True)
