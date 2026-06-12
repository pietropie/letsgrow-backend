"""hub_satellite_fields

Adiciona suporte a arquitetura Hub+Satelite ESP-NOW em sensor_devices:
  - module_type  VARCHAR(20)  nullable  ('hub' | 'satellite' | 'standalone')
  - hub_mac      VARCHAR(17)  nullable  FK self-referencial para sensor_devices.esp32_mac
  - is_paired    BOOLEAN      NOT NULL  default FALSE

Revision ID: j5k6l7m8n9o0
Revises: i4j5k6l7m8n9
Create Date: 2026-06-12 00:00:00.000000
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "j5k6l7m8n9o0"
down_revision: Union[str, None] = "i4j5k6l7m8n9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # module_type: identifica o papel do dispositivo na topologia ESP-NOW
    # Valores esperados: 'hub' | 'satellite' | 'standalone' (default legado)
    op.add_column(
        "sensor_devices",
        sa.Column(
            "module_type",
            sa.String(length=20),
            nullable=True,
            comment="hub | satellite | standalone",
        ),
    )

    # hub_mac: referencia o MAC do hub pai (apenas para satellites)
    # Armazenamos como VARCHAR simples com FK soft (sem constraint de FK
    # em banco) porque o hub pode ainda nao estar registrado no momento
    # em que o satellite e descoberto — a integridade e garantida em
    # camada de aplicacao.
    op.add_column(
        "sensor_devices",
        sa.Column(
            "hub_mac",
            sa.String(length=17),
            nullable=True,
            comment="MAC do hub pai (somente satellites)",
        ),
    )
    op.create_index(
        "ix_sensor_devices_hub_mac",
        "sensor_devices",
        ["hub_mac"],
    )

    # is_paired: FALSE enquanto o grower nao atribuiu o dispositivo a uma planta.
    # Dispositivos descobertos via MQTT discovery ficam com is_paired=FALSE ate
    # o usuario confirmar pelo endpoint PATCH /iot/devices/{id}.
    op.add_column(
        "sensor_devices",
        sa.Column(
            "is_paired",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
            comment="FALSE = dispositivo descoberto mas ainda nao atribuido",
        ),
    )

    # Dispositivos pre-existentes (criados manualmente via POST /iot/devices)
    # ja estao operacionais — marcamos como paired e standalone.
    op.execute(
        "UPDATE sensor_devices SET is_paired = TRUE, module_type = 'standalone' "
        "WHERE is_paired = FALSE"
    )


def downgrade() -> None:
    op.drop_index("ix_sensor_devices_hub_mac", table_name="sensor_devices")
    op.drop_column("sensor_devices", "is_paired")
    op.drop_column("sensor_devices", "hub_mac")
    op.drop_column("sensor_devices", "module_type")
