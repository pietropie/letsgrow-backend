import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field


class DeviceRegister(BaseModel):
    plant_id: uuid.UUID | None = None
    name: str
    esp32_mac: str
    sensors_config: dict = {}
    # Optional topology fields when registering a hub or standalone manually
    module_type: Literal["hub", "satellite", "standalone"] | None = None
    hub_mac: str | None = None


class DevicePatch(BaseModel):
    """
    Payload for PATCH /iot/devices/{id}.

    The grower uses this endpoint to assign a discovered (pending) satellite
    to a plant and give it a human-readable name.  All fields are optional so
    the endpoint can also be used for partial updates on already-paired devices
    (e.g. renaming).
    """

    plant_id: uuid.UUID | None = Field(
        default=None,
        description="UUID da planta a que o sensor sera atribuido.",
    )
    name: str | None = Field(
        default=None,
        max_length=60,
        description="Nome legivel para o dispositivo, ex: 'Sensor solo vaso 1'.",
    )
    module_type: Literal["hub", "satellite", "standalone"] | None = None
    sensors_config: dict | None = None


class DeviceResponse(BaseModel):
    id: uuid.UUID
    plant_id: uuid.UUID | None
    # grow_id is resolved server-side via plant.grow_id (not stored on sensor_devices)
    grow_id: uuid.UUID | None = None
    name: str
    esp32_mac: str
    firmware_version: str | None
    sensors_config: dict
    # Hub+Satellite topology
    module_type: str | None
    hub_mac: str | None
    is_paired: bool
    # Status
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
