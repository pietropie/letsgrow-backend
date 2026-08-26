"""grow as root hierarchy

Reverte a planta-como-raiz: toda planta pertence a um grow.
- backfill de plants.grow_id (label existente ou "Meu grow")
- grow_id NOT NULL + FK RESTRICT (arquivar, nao apagar em cascata)
- campos indoor/outdoor no grow (alvos, fotoperiodo, exhaust, fans, lat/lon)
- sensor_devices.grow_id (ambiente no grow; solo na planta)

Revision ID: t6u7v8w9x0y1
Revises: t5u6v7w8x9y0
Create Date: 2026-08-26
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

revision = "t6u7v8w9x0y1"
down_revision = "t5u6v7w8x9y0"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ------------------------------------------------------------------
    # 1. Campos novos em grows
    # ------------------------------------------------------------------
    op.add_column("grows", sa.Column("target_temp_c", sa.Float(), nullable=True))
    op.add_column("grows", sa.Column("target_humidity_rh", sa.Float(), nullable=True))
    op.add_column("grows", sa.Column("photoperiod_on_at", sa.Time(), nullable=True))
    op.add_column("grows", sa.Column("exhaust_cfm", sa.Integer(), nullable=True))
    op.add_column("grows", sa.Column("exhaust_location", sa.String(length=20), nullable=True))
    op.add_column("grows", sa.Column("fans", JSONB(), nullable=True))
    op.add_column("grows", sa.Column("lat", sa.Float(), nullable=True))
    op.add_column("grows", sa.Column("lon", sa.Float(), nullable=True))
    op.add_column("grows", sa.Column("sun_exposure", sa.String(length=20), nullable=True))
    op.add_column("grows", sa.Column("rain_protection", sa.String(length=20), nullable=True))

    # ------------------------------------------------------------------
    # 2. Backfill plants.grow_id
    # ------------------------------------------------------------------
    op.execute(
        """
        UPDATE plants p
        SET grow_id = g.id
        FROM grows g
        WHERE p.grow_id IS NULL
          AND p.grow_label IS NOT NULL
          AND btrim(p.grow_label) <> ''
          AND g.user_id = p.user_id
          AND g.name = p.grow_label
        """
    )
    op.execute(
        """
        INSERT INTO grows (
            id, user_id, name, grow_type, status, start_date,
            has_environment_sensors, created_at, updated_at
        )
        SELECT gen_random_uuid(), p.user_id, p.grow_label, 'indoor', 'active',
               CURRENT_DATE, false, NOW(), NOW()
        FROM (
            SELECT DISTINCT user_id, grow_label
            FROM plants
            WHERE grow_id IS NULL
              AND grow_label IS NOT NULL
              AND btrim(grow_label) <> ''
        ) p
        """
    )
    op.execute(
        """
        UPDATE plants p
        SET grow_id = g.id
        FROM grows g
        WHERE p.grow_id IS NULL
          AND p.grow_label IS NOT NULL
          AND btrim(p.grow_label) <> ''
          AND g.user_id = p.user_id
          AND g.name = p.grow_label
        """
    )
    op.execute(
        """
        INSERT INTO grows (
            id, user_id, name, grow_type, status, start_date,
            has_environment_sensors, created_at, updated_at
        )
        SELECT gen_random_uuid(), x.user_id, 'Meu grow', 'indoor', 'active',
               CURRENT_DATE, false, NOW(), NOW()
        FROM (
            SELECT DISTINCT user_id FROM plants WHERE grow_id IS NULL
        ) x
        """
    )
    op.execute(
        """
        UPDATE plants p
        SET grow_id = g.id
        FROM grows g
        WHERE p.grow_id IS NULL
          AND g.user_id = p.user_id
          AND g.name = 'Meu grow'
        """
    )

    # ------------------------------------------------------------------
    # 3. grow_id obrigatorio + FK RESTRICT
    # ------------------------------------------------------------------
    op.alter_column("plants", "grow_id", existing_type=UUID(as_uuid=True), nullable=False)
    op.drop_constraint("fk_plants_grow_id", "plants", type_="foreignkey")
    op.create_foreign_key(
        "fk_plants_grow_id",
        "plants",
        "grows",
        ["grow_id"],
        ["id"],
        ondelete="RESTRICT",
    )

    # ------------------------------------------------------------------
    # 4. sensor_devices.grow_id
    # ------------------------------------------------------------------
    op.add_column(
        "sensor_devices",
        sa.Column("grow_id", UUID(as_uuid=True), nullable=True),
    )
    op.execute(
        """
        UPDATE sensor_devices sd
        SET grow_id = p.grow_id
        FROM plants p
        WHERE sd.plant_id = p.id
          AND sd.grow_id IS NULL
        """
    )
    op.create_index("ix_sensor_devices_grow_id", "sensor_devices", ["grow_id"])
    op.create_foreign_key(
        "fk_sensor_devices_grow_id",
        "sensor_devices",
        "grows",
        ["grow_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_sensor_devices_grow_id", "sensor_devices", type_="foreignkey")
    op.drop_index("ix_sensor_devices_grow_id", table_name="sensor_devices")
    op.drop_column("sensor_devices", "grow_id")

    op.drop_constraint("fk_plants_grow_id", "plants", type_="foreignkey")
    op.alter_column("plants", "grow_id", existing_type=UUID(as_uuid=True), nullable=True)
    op.create_foreign_key(
        "fk_plants_grow_id",
        "plants",
        "grows",
        ["grow_id"],
        ["id"],
        ondelete="SET NULL",
    )

    op.drop_column("grows", "rain_protection")
    op.drop_column("grows", "sun_exposure")
    op.drop_column("grows", "lon")
    op.drop_column("grows", "lat")
    op.drop_column("grows", "fans")
    op.drop_column("grows", "exhaust_location")
    op.drop_column("grows", "exhaust_cfm")
    op.drop_column("grows", "photoperiod_on_at")
    op.drop_column("grows", "target_humidity_rh")
    op.drop_column("grows", "target_temp_c")
