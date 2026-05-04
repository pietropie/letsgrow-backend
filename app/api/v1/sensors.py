import uuid
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.grow import Grow
from app.models.sensor import SensorDevice, SensorReading
from app.models.user import User
from app.schemas.sensor import SensorReadingResponse, SensorSummary
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
        .join(Grow, SensorDevice.grow_id == Grow.id)
        .where(SensorDevice.id == device_id, Grow.user_id == user_id)
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
