"""create_feature_flags_table

Cria a tabela feature_flags e insere as flags iniciais do produto.

Flags iniciais:
  - plan_switch        Troca de plano            enabled=False
  - iot_pairing        Pairing de sensores IoT   enabled=True
  - bob_tips           Dicas do Bob              enabled=True
  - grow_environment   Configuracao de ambiente  enabled=True

O endpoint público GET /feature-flags retorna {key, enabled} para
o app mobile decidir quais telas exibir. O admin gerencia via
GET /admin/feature-flags e PATCH /admin/feature-flags/{key}.

Revision ID: l7m8n9o0p1q2
Revises: k6l7m8n9o0p1
Create Date: 2026-06-15 00:01:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "l7m8n9o0p1q2"
down_revision: Union[str, None] = "k6l7m8n9o0p1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Flags iniciais: (key, name, description, enabled)
_SEED_FLAGS = [
    (
        "plan_switch",
        "Troca de plano",
        "Permite ao usuário trocar de plano diretamente pelo app",
        False,
    ),
    (
        "iot_pairing",
        "Pairing de sensores IoT",
        "Habilita o fluxo de descoberta e pareamento de dispositivos ESP32",
        True,
    ),
    (
        "bob_tips",
        "Dicas do Bob",
        "Exibe as dicas e sugestoes do assistente Bob na interface",
        True,
    ),
    (
        "grow_environment",
        "Configuracao de ambiente do grow",
        "Permite configurar temperatura, umidade e iluminacao do ambiente",
        True,
    ),
]


def upgrade() -> None:
    feature_flags_table = op.create_table(
        "feature_flags",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("gen_random_uuid()"),
            nullable=False,
        ),
        sa.Column(
            "key",
            sa.String(length=100),
            unique=True,
            nullable=False,
            comment="Identificador unico da flag (ex: plan_switch)",
        ),
        sa.Column(
            "name",
            sa.String(length=200),
            nullable=False,
            comment="Nome legivel exibido no painel admin",
        ),
        sa.Column(
            "description",
            sa.Text(),
            nullable=True,
            comment="Explicacao do que a flag controla",
        ),
        sa.Column(
            "enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="TRUE = feature ativa para todos os usuarios",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("NOW()"),
            nullable=False,
        ),
    )

    op.create_index("ix_feature_flags_key", "feature_flags", ["key"], unique=True)

    # Seed com as flags iniciais do produto
    op.bulk_insert(
        feature_flags_table,
        [
            {
                "key": key,
                "name": name,
                "description": description,
                "enabled": enabled,
            }
            for key, name, description, enabled in _SEED_FLAGS
        ],
    )


def downgrade() -> None:
    op.drop_index("ix_feature_flags_key", table_name="feature_flags")
    op.drop_table("feature_flags")
