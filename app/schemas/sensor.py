import uuid
from datetime import datetime

from pydantic import BaseModel


class DeviceRegister(BaseModel):
    plant_id: uuid.UUID | None = None
    name: str
    esp32_mac: str
    sensors_config: dict = {}


class DeviceResponse(BaseModel):
    id: uuid.UUID
    plant_id: uuid.UUID | None
    name: str
    esp32_mac: str
    firmware_version: str | None
    sensors_config: dict
    is_online: bool
    last_seen_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class SensorReadingCreate(BaseModel):
    device_id: uuid.UUID
    recorded_at: datetime
    temp_air: float | None = None
    humidity_air: float | None = None
    co2_ppm: int | None = None
    soil_moisture_pct: float | None = None
    ph_solution: float | None = None
    ec_ms_cm: float | None = None
    vpd_kpa: float | None = None
    lux: int | None = None
    temp_root: float | None = None
    light_leak: bool | None = None


class SensorReadingResponse(SensorReadingCreate):
    id: uuid.UUID

    model_config = {"from_attributes": True}


class SensorSummary(BaseModel):
    device_id: uuid.UUID
    latest: SensorReadingResponse | None
    avg_temp_air_24h: float | None
    avg_humidity_24h: float | None
    avg_vpd_24h: float | None
    avg_co2_24h: float | None
    alerts: list[str]
