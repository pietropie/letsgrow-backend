import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator


class DeviceRegister(BaseModel):
    plant_id: uuid.UUID | None = None
    grow_id: uuid.UUID | None = None
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
        description="UUID da planta (solo/nutricao).",
    )
    grow_id: uuid.UUID | None = Field(
        default=None,
        description="UUID do grow (ambiente/PPFD/hub). Inferido da planta se omitido.",
    )
    name: str | None = Field(
        default=None,
        max_length=60,
        description="Nome legivel para o dispositivo, ex: 'Sensor solo vaso 1'.",
    )
    module_type: Literal["hub", "satellite", "standalone"] | None = None
    sensors_config: dict | None = None
    # Tipo legível do satélite (para aparelhos antigos sem module_type no sensors_config)
    sensor_kind: Literal["air", "soil", "light", "solution", "relay", "co2", "ph", "root"] | None = None
    # Hub: tensão da tomada (127 ou 220); null volta a "não informada" (conta como 127)
    mains_voltage: Literal[127, 220] | None = None


class DeviceResponse(BaseModel):
    id: uuid.UUID
    plant_id: uuid.UUID | None
    grow_id: uuid.UUID | None = None
    name: str
    esp32_mac: str
    firmware_version: str | None
    sensors_config: dict
    # Hub+Satellite topology
    module_type: str | None
    hub_mac: str | None
    is_paired: bool
    # Tipo legível: air | soil | light | solution | relay | hub (null: ainda sem tipo conhecido)
    sensor_kind: str | None = None
    kind_label: str | None = None
    description: str | None = None
    icon: str | None = None
    # Foto do modelo do sensor; null = o app mostra o ícone do tipo
    photo_url: str | None = None
    mains_voltage: int | None = None
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
    # Calculado do EC na escala 500 (ppm = EC em mS/cm × 500)
    ppm: int | None = None

    model_config = {"from_attributes": True}

    @model_validator(mode="after")
    def _ppm(self):
        if self.ppm is None and self.ec_ms_cm is not None:
            self.ppm = round(self.ec_ms_cm * 500)
        return self


class SensorSummary(BaseModel):
    device_id: uuid.UUID
    latest: SensorReadingResponse | None
    avg_temp_air_24h: float | None
    avg_humidity_24h: float | None
    avg_vpd_24h: float | None
    avg_co2_24h: float | None
    alerts: list[str]


# ---------------------------------------------------------------------------
# Onboarding do Hub por BLE (contrato hub-onboarding-ble)
# ---------------------------------------------------------------------------

class HubClaimCodeCreate(BaseModel):
    grow_id: uuid.UUID


class HubClaimCodeResponse(BaseModel):
    code: str
    expires_at: datetime


class HubActivateRequest(BaseModel):
    """Enviado pelo próprio Hub. Campos soltos de propósito: formato inválido
    responde 400 {"detail": "bad_request"} (o firmware mostra esse erro), não 422."""

    mac: str = ""
    code: str = ""
    fw: str | None = None


class HubActivateResponse(BaseModel):
    grow_id: uuid.UUID
    mqtt_password: str
    mqtt_host: str
    mqtt_port: int


class HubPairingOpen(BaseModel):
    ttl_s: int = Field(default=60, ge=10, le=300)


class HubPairingResponse(BaseModel):
    hub_id: uuid.UUID
    ttl_s: int
    expires_at: datetime
