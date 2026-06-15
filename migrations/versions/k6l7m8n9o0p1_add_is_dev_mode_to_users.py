"""add_is_dev_mode_to_users

Adiciona campo is_dev_mode na tabela users.

Quando TRUE, o usuário recebe acesso a features beta ainda não
lançadas para o público geral (ex: troca de plano, telas beta).
O campo é exposto em GET /users/me e controlado pelo admin via
PATCH /admin/customers/{user_id}/dev-mode.

Revision ID: k6l7m8n9o0p1
Revises: j5k6l7m8n9o0
Create Date: 2026-06-15 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "k6l7m8n9o0p1"
down_revision: Union[str, None] = "j5k6l7m8n9o0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "is_dev_mode",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="Acesso a features beta quando TRUE",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "is_dev_mode")
