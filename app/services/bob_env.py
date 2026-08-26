"""Leitura ao vivo de ambiente para o motor do Bob (I/O)."""
from __future__ import annotations

import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.plant import Plant
from app.models.sensor import SensorDevice, SensorReading


async def load_latest_sensor_env(
    db: AsyncSession,
    plant_id: uuid.UUID,
    grow_id: uuid.UUID | None = None,
) -> tuple[float | None, float | None]:
    """Ultima temp/umidade: sensor da planta ou do grow."""
    resolved_grow_id = grow_id
    if resolved_grow_id is None:
        resolved_grow_id = (
            await db.execute(select(Plant.grow_id).where(Plant.id == plant_id))
        ).scalar_one_or_none()

    conditions = [SensorDevice.plant_id == plant_id]
    if resolved_grow_id is not None:
        conditions.append(SensorDevice.grow_id == resolved_grow_id)

    result = await db.execute(
        select(SensorReading.temp_air, SensorReading.humidity_air)
        .join(SensorDevice, SensorDevice.id == SensorReading.device_id)
        .where(or_(*conditions))
        .order_by(SensorReading.recorded_at.desc())
        .limit(1)
    )
    row = result.first()
    if row is None:
        return None, None
    return row[0], row[1]
