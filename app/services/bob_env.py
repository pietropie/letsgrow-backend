"""Leitura de ambiente para o motor do Bob (I/O).

Usa o mesmo resolvedor das telas de ambiente (grow_env.latest_grow_measurements):
a medicao mais recente de cada indicador entre sensores, registros do diario e
registros manuais do ambiente.
"""
from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.plant import Plant
from app.schemas.grow import EnvironmentMeasurement
from app.services.grow_env import latest_grow_measurements

_VALUE_KEYS = {"temperature": "temperature_c", "humidity": "humidity_rh"}


def measurement_evidence(metric: str, measurement: EnvironmentMeasurement, grow_id: uuid.UUID) -> dict:
    return {
        "kind": measurement.source_kind,
        "id": str(measurement.source_id),
        "observed_at": measurement.observed_at.isoformat() if measurement.observed_at else None,
        "source": measurement.source,
        "plant_id": None,
        "grow_id": str(grow_id),
        "values": {_VALUE_KEYS[metric]: measurement.value},
    }


async def load_latest_env_with_evidence(
    db: AsyncSession,
    plant_id: uuid.UUID,
    grow_id: uuid.UUID | None = None,
) -> tuple[float | None, float | None, dict[str, dict]]:
    """Ultima temperatura/umidade do ambiente da planta e a evidencia de cada uma."""
    resolved_grow_id = grow_id
    if resolved_grow_id is None:
        resolved_grow_id = (
            await db.execute(select(Plant.grow_id).where(Plant.id == plant_id))
        ).scalar_one_or_none()
    if resolved_grow_id is None:
        return None, None, {}

    readings = await latest_grow_measurements(db, resolved_grow_id)
    evidence = {
        metric: measurement_evidence(metric, readings[metric], resolved_grow_id)
        for metric in _VALUE_KEYS
        if readings[metric].value is not None
    }
    return readings["temperature"].value, readings["humidity"].value, evidence


async def load_latest_env(
    db: AsyncSession,
    plant_id: uuid.UUID,
    grow_id: uuid.UUID | None = None,
) -> tuple[float | None, float | None]:
    """Ultima temperatura/umidade do ambiente da planta (sem evidencia)."""
    temp, humidity, _ = await load_latest_env_with_evidence(db, plant_id, grow_id)
    return temp, humidity
