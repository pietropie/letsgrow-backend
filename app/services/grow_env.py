"""Snapshot de ambiente de um grow: sensores do grow + eventos das plantas."""
from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import GrowEvent
from app.models.plant import Plant
from app.models.sensor import SensorDevice, SensorReading
from app.schemas.grow import GrowEnvSnapshot

_WATERING_TYPES = ("rega", "watering", "nutrients", "feeding")


async def latest_grow_env(db: AsyncSession, grow_id: uuid.UUID) -> GrowEnvSnapshot:
    reading_row = (
        await db.execute(
            select(SensorReading)
            .join(SensorDevice, SensorDevice.id == SensorReading.device_id)
            .outerjoin(Plant, SensorDevice.plant_id == Plant.id)
            .where(
                or_(
                    SensorDevice.grow_id == grow_id,
                    Plant.grow_id == grow_id,
                )
            )
            .order_by(SensorReading.recorded_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    last_event_at = (
        await db.execute(
            select(func.max(GrowEvent.event_date))
            .join(Plant, Plant.id == GrowEvent.plant_id)
            .where(Plant.grow_id == grow_id)
        )
    ).scalar_one()

    last_watering_at = (
        await db.execute(
            select(func.max(GrowEvent.event_date))
            .join(Plant, Plant.id == GrowEvent.plant_id)
            .where(
                Plant.grow_id == grow_id,
                GrowEvent.event_type.in_(_WATERING_TYPES),
            )
        )
    ).scalar_one()

    temp_c = reading_row.temp_air if reading_row else None
    humidity_rh = reading_row.humidity_air if reading_row else None
    vpd_kpa = reading_row.vpd_kpa if reading_row else None
    lux = reading_row.lux if reading_row else None
    recorded_at: datetime | None = reading_row.recorded_at if reading_row else None

    if temp_c is None or humidity_rh is None:
        event_env = (
            await db.execute(
                select(GrowEvent)
                .join(Plant, Plant.id == GrowEvent.plant_id)
                .where(
                    Plant.grow_id == grow_id,
                    or_(
                        GrowEvent.temperature_c.is_not(None),
                        GrowEvent.humidity_rh.is_not(None),
                    ),
                )
                .order_by(GrowEvent.event_date.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if event_env is not None:
            if temp_c is None:
                temp_c = event_env.temperature_c
            if humidity_rh is None:
                humidity_rh = event_env.humidity_rh
            if recorded_at is None:
                recorded_at = event_env.event_date

    return GrowEnvSnapshot(
        temp_c=temp_c,
        humidity_rh=humidity_rh,
        vpd_kpa=vpd_kpa,
        lux=lux,
        recorded_at=recorded_at,
        last_event_at=last_event_at,
        last_watering_at=last_watering_at,
    )
