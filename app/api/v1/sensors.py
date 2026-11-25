import uuid
from datetime import datetime, timedelta, timezone

from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db

from app.models.grow import Grow
from app.models.plant import Plant
from app.models.sensor import SensorDevice, SensorReading
from app.models.user import User
from app.schemas.sensor import SensorReadingResponse, SensorSummary
from app.schemas.sensor_panel import SensorHistoryOut
from app.services import ideal_ranges as ranges
from app.services import sensor_catalog as catalog
from app.services import sensor_panel
from app.services.auth import get_current_user

router = APIRouter()

# VPD / environment thresholds for alert generation
_ALERTS = [
    ("temp_air", 30, "gt", "Temperatura do ar acima de 30°C"),
    ("temp_air", 18, "lt", "Temperatura do ar abaixo de 18°C"),
    ("humidity_air", 70, "gt", "Umidade relativa acima de 70% — risco de mofo"),
    ("humidity_air", 30, "lt", "Umidade relativa abaixo de 30%"),
    ("co2_ppm", 1500, "gt", "CO₂ acima de 1500ppm"),
    ("co2_ppm", 400, "lt", "CO₂ abaixo de 400ppm"),
    ("ph_solution", 7.0, "gt", "pH da solução acima de 7.0"),
    ("ph_solution", 5.5, "lt", "pH da solução abaixo de 5.5"),
]


def _generate_alerts(reading: SensorReading) -> list[str]:
    alerts = []
    for field, threshold, op, message in _ALERTS:
        value = getattr(reading, field)
        if value is None:
            continue
        if op == "gt" and value > threshold:
            alerts.append(message)
        elif op == "lt" and value < threshold:
            alerts.append(message)
    return alerts


async def _get_device_or_404(
    device_id: uuid.UUID, user_id: uuid.UUID, db: AsyncSession
) -> SensorDevice:
    result = await db.execute(
        select(SensorDevice)
        .outerjoin(Plant, SensorDevice.plant_id == Plant.id)
        .outerjoin(Grow, SensorDevice.grow_id == Grow.id)
        .where(
            SensorDevice.id == device_id,
            or_(Plant.user_id == user_id, Grow.user_id == user_id),
        )
    )
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dispositivo não encontrado")
    return device


@router.get("/{device_id}/latest", response_model=SensorReadingResponse | None)
async def latest_reading(
    device_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_device_or_404(device_id, current_user.id, db)
    result = await db.execute(
        select(SensorReading)
        .where(SensorReading.device_id == device_id)
        .order_by(SensorReading.recorded_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


@router.get("/{device_id}/readings", response_model=list[SensorReadingResponse])
async def list_readings(
    device_id: uuid.UUID,
    hours: int = 24,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_device_or_404(device_id, current_user.id, db)
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    result = await db.execute(
        select(SensorReading)
        .where(SensorReading.device_id == device_id, SensorReading.recorded_at >= since)
        .order_by(SensorReading.recorded_at.asc())
    )
    return result.scalars().all()


@router.get("/{device_id}/summary", response_model=SensorSummary)
async def sensor_summary(
    device_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_device_or_404(device_id, current_user.id, db)
    since = datetime.now(timezone.utc) - timedelta(hours=24)

    agg = await db.execute(
        select(
            func.avg(SensorReading.temp_air),
            func.avg(SensorReading.humidity_air),
            func.avg(SensorReading.vpd_kpa),
            func.avg(SensorReading.co2_ppm),
        ).where(SensorReading.device_id == device_id, SensorReading.recorded_at >= since)
    )
    avgs = agg.one()

    latest_result = await db.execute(
        select(SensorReading)
        .where(SensorReading.device_id == device_id)
        .order_by(SensorReading.recorded_at.desc())
        .limit(1)
    )
    latest = latest_result.scalar_one_or_none()
    alerts = _generate_alerts(latest) if latest else []

    return SensorSummary(
        device_id=device_id,
        latest=latest,
        avg_temp_air_24h=round(avgs[0], 1) if avgs[0] else None,
        avg_humidity_24h=round(avgs[1], 1) if avgs[1] else None,
        avg_vpd_24h=round(avgs[2], 2) if avgs[2] else None,
        avg_co2_24h=round(avgs[3]) if avgs[3] else None,
        alerts=alerts,
    )


HistoryMetric = Literal["temp_air", "humidity_air", "vpd_kpa", "lux", "soil_moisture_pct", "ppm", "ec_ms_cm",
                        "co2_ppm", "ph_solution", "temp_root"]


@router.get("/{device_id}/history", response_model=SensorHistoryOut)
async def sensor_history(
    device_id: uuid.UUID,
    metric: HistoryMetric,
    window: Literal["24h", "3d", "7d"] = Query(default="24h"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Leituras agregadas para o mini-histórico: 24 h em 24 barras de 1 h, 3 dias em 18 de
    4 h e 7 dias em 14 de 12 h. Cada barra traz média, mínimo, máximo e o estado da média
    em relação à faixa ideal (a do ambiente ou a sugerida pela fase)."""
    device = await _get_device_or_404(device_id, current_user.id, db)
    now = datetime.now(timezone.utc)
    grow_id = device.grow_id
    plant = await db.get(Plant, device.plant_id) if device.plant_id else None
    if grow_id is None and plant is not None:
        grow_id = plant.grow_id
    band = None
    if grow_id is not None and metric in ranges.RANGE_METRICS:
        phase, _ = await sensor_panel.grow_phase(db, grow_id)
        if plant is not None and plant.current_phase not in (None, "unknown"):
            phase = plant.current_phase
        band = ranges.effective(metric, phase, await sensor_panel.custom_ranges(db, grow_id))
    readings = await sensor_panel.readings_since(db, device_id, now - sensor_panel.WINDOWS[window][0])
    data = sensor_panel.aggregate(readings, metric, window, now, band)
    return {**data, "device_id": device_id, "unit": catalog.METRICS[metric].unit,
            "range": sensor_panel.range_out(band)}
