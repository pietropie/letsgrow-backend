"""Resolve each environment indicator independently, with its evidence and date."""
from __future__ import annotations

import math
import uuid
from datetime import datetime, timezone

from sqlalchemy import Float, and_, case, cast, func, literal, or_, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.environment_reading import EnvironmentReading
from app.models.event import GrowEvent
from app.models.plant import Plant
from app.models.sensor import SensorDevice, SensorReading
from app.schemas.grow import EnvironmentMeasurement, GrowEnvSnapshot

_WATERING_TYPES = ("rega", "watering", "nutrients", "feeding")
_UNITS = {"temperature": "°C", "humidity": "%", "light": "lux", "vpd": "kPa"}


def _utc(value: datetime) -> datetime:
    # Historical naive timestamps in the database represent UTC.
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


async def latest_grow_measurements(
    db: AsyncSession, grow_id: uuid.UUID, *, now: datetime | None = None,
) -> dict[str, EnvironmentMeasurement]:
    """Newest non-null value per metric across manual records and sensors.

    One query ranks the candidates independently. On equal timestamps sensors
    take precedence, then the source ID provides a deterministic tie breaker.
    A direct device grow assignment takes precedence over its legacy plant link.
    Callers must authorize the grow before calling this internal resolver.
    """
    cutoff = _utc(now or datetime.now(timezone.utc))
    device_scope = or_(
        SensorDevice.grow_id == grow_id,
        and_(SensorDevice.grow_id.is_(None), Plant.grow_id == grow_id),
    )
    candidates = []
    for metric, column in (
        ("temperature", SensorReading.temp_air), ("humidity", SensorReading.humidity_air),
        ("light", SensorReading.lux), ("vpd", SensorReading.vpd_kpa),
    ):
        candidates.append(
            select(literal(metric).label("metric"), cast(column, Float).label("value"),
                   literal("sensor").label("source"), literal("sensor_reading").label("source_kind"),
                   SensorReading.id.label("source_id"),
                   SensorReading.recorded_at.label("observed_at"))
            .select_from(SensorReading)
            .join(SensorDevice, SensorDevice.id == SensorReading.device_id)
            .outerjoin(Plant, Plant.id == SensorDevice.plant_id)
            .where(device_scope, column.is_not(None), SensorReading.recorded_at <= cutoff,
                   column > float('-inf'), column < float('inf'))
            .order_by(SensorReading.recorded_at.desc(), SensorReading.id.desc()).limit(1)
        )
    for metric, column in (("temperature", GrowEvent.temperature_c), ("humidity", GrowEvent.humidity_rh)):
        candidates.append(
            select(literal(metric).label("metric"), cast(column, Float).label("value"),
                   literal("manual").label("source"), literal("event").label("source_kind"),
                   GrowEvent.id.label("source_id"),
                   GrowEvent.event_date.label("observed_at"))
            .select_from(GrowEvent).join(Plant, Plant.id == GrowEvent.plant_id)
            .where(Plant.grow_id == grow_id, column.is_not(None), GrowEvent.event_date <= cutoff,
                   column > float('-inf'), column < float('inf'))
            .order_by(GrowEvent.event_date.desc(), GrowEvent.id.desc()).limit(1)
        )
    for metric, column in (("temperature", EnvironmentReading.temperature_c), ("humidity", EnvironmentReading.humidity_rh)):
        candidates.append(
            select(literal(metric).label("metric"), cast(column, Float).label("value"),
                   literal("manual").label("source"), literal("environment_reading").label("source_kind"),
                   EnvironmentReading.id.label("source_id"), EnvironmentReading.observed_at.label("observed_at"))
            .where(EnvironmentReading.grow_id == grow_id, column.is_not(None),
                   EnvironmentReading.observed_at <= cutoff,
                   column > float('-inf'), column < float('inf'))
            .order_by(EnvironmentReading.observed_at.desc(), EnvironmentReading.id.desc()).limit(1)
        )
    combined = union_all(*(select(candidate.subquery()) for candidate in candidates)).subquery()
    # Exclude non-finite readings without imposing agronomic ranges.
    valid = select(combined).where(
        combined.c.value > float('-inf'), combined.c.value < float('inf'),
    ).subquery()
    ranked = select(valid, func.row_number().over(
        partition_by=valid.c.metric,
        order_by=(valid.c.observed_at.desc(), valid.c.source.desc(), valid.c.source_id.desc()),
    ).label("position")).subquery()
    rows = (await db.execute(select(ranked).where(ranked.c.position == 1))).mappings().all()
    result = {metric: EnvironmentMeasurement(unit=unit) for metric, unit in _UNITS.items()}
    for row in rows:
        if not math.isfinite(row["value"]):
            continue
        result[row["metric"]] = EnvironmentMeasurement(
            value=row["value"], unit=_UNITS[row["metric"]], source=row["source"],
            source_id=row["source_id"], source_kind=row["source_kind"],
            observed_at=_utc(row["observed_at"]),
        )
    return result


async def latest_grow_env(db: AsyncSession, grow_id: uuid.UUID) -> GrowEnvSnapshot:
    readings = await latest_grow_measurements(db, grow_id)
    activity = (await db.execute(
        select(func.max(GrowEvent.event_date), func.max(case(
            (GrowEvent.event_type.in_(_WATERING_TYPES), GrowEvent.event_date), else_=None,
        )))
        .join(Plant, Plant.id == GrowEvent.plant_id).where(Plant.grow_id == grow_id)
    )).one()
    dates = {reading.observed_at for reading in readings.values() if reading.value is not None}
    return GrowEnvSnapshot(
        **readings,
        temp_c=readings["temperature"].value,
        humidity_rh=readings["humidity"].value,
        lux=int(readings["light"].value) if readings["light"].value is not None else None,
        vpd_kpa=readings["vpd"].value,
        recorded_at=next(iter(dates)) if len(dates) == 1 else None,
        last_event_at=_utc(activity[0]) if activity[0] else None,
        last_watering_at=_utc(activity[1]) if activity[1] else None,
    )
