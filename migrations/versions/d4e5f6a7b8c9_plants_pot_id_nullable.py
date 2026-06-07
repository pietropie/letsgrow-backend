"""make plants.pot_id nullable — plants no longer require a pot

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-07 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Plant deixou de ter relação obrigatória com Pot (refatoração: Plant como
    # entidade raiz). A coluna legada pot_id é preservada para não perder
    # histórico, mas precisa aceitar NULL para permitir a criação de novas plantas.
    op.alter_column('plants', 'pot_id', existing_type=UUID(as_uuid=True), nullable=True)


def downgrade() -> None:
    op.alter_column('plants', 'pot_id', existing_type=UUID(as_uuid=True), nullable=False)
