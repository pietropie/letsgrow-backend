import uuid
from datetime import date, datetime, time
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# ---------------------------------------------------------------------------
# Constantes de enum (documentam os valores aceitos)
# ---------------------------------------------------------------------------
LightType = Literal["led_quantum_board", "led_cob", "hps_hid", "cmh", "fluorescent", "other"]
PhotoperiodHours = Literal["18/6", "12/12", "20/4", "24/0", "custom"]
ExhaustType = Literal["inline_fan", "axial_fan", "pc_fans", "none"]
IntakeType = Literal["active_fan", "passive", "none"]
PotType = Literal["fabric", "plastic", "air_pot", "other"]
SubstrateType = Literal["mineral_soil", "coco", "organic_supersoil", "hydro_dwc", "other"]
SunExposure = Literal["full", "partial", "shade"]
RainProtection = Literal["none", "cover", "greenhouse"]
ExhaustLocation = Literal["copa", "base"]

LOCATION_DECIMALS = 2  # ~1 km: o servidor nunca guarda a coordenada exata (privacidade)


def _round_coordinate(value: float | None) -> float | None:
    return None if value is None else round(value, LOCATION_DECIMALS)


def _clean_label(value: str | None) -> str | None:
    value = " ".join(value.split()) if value is not None else None
    return value or None


class _LocationFields(BaseModel):
    """Local do ambiente: localizacao aproximada ou cidade escolhida em /weather/places."""

    lat: float | None = Field(default=None, ge=-90, le=90, description="Arredondada a 2 casas pelo servidor.")
    lon: float | None = Field(default=None, ge=-180, le=180, description="Arredondada a 2 casas pelo servidor.")
    location_label: str | None = Field(default=None, max_length=120, description='Nome legivel, ex. "Campinas, SP".')

    round_coordinates = field_validator("lat", "lon")(_round_coordinate)
    clean_location_label = field_validator("location_label")(_clean_label)

    @model_validator(mode="after")
    def _coordinates_go_together(self):
        sent = {"lat", "lon"} & self.model_fields_set
        if sent and (len(sent) == 1 or (self.lat is None) != (self.lon is None)):
            raise ValueError("Envie lat e lon juntos (os dois com valor ou os dois null).")
        return self


class GrowCreate(_LocationFields):
    name: str
    grow_type: str = "indoor"
    start_date: date = Field(default_factory=date.today)
    tent_width_cm: int | None = None
    tent_depth_cm: int | None = None
    tent_height_cm: int | None = None
    lighting_watts: int | None = None
    light_type: str | None = None
    light_distance_cm: int | None = None
    # Derivado pelo servidor; clientes antigos podem enviar "N/M"
    photoperiod_hours: str | None = None
    photoperiod_on_at: time | None = None
    photoperiod_light_minutes: int | None = Field(
        default=None, ge=1, le=1440,
        description="Duracao da luz em minutos (1-1440). O servidor deriva photoperiod_hours. "
                    "Exige photoperiod_on_at (no corpo ou ja salvo), exceto 1440 (24/0). null volta a 'nao informado'.",
    )
    light_leak_controlled: bool | None = None
    exhaust_type: str | None = None
    exhaust_cfm: int | None = None
    exhaust_location: str | None = None
    carbon_filter: bool | None = None
    intake_type: str | None = None
    internal_circulation_fans: int | None = None
    negative_pressure: bool | None = None
    fans: list[dict[str, Any]] | None = None
    air_conditioning: bool | None = None
    dehumidifier: bool | None = None
    humidifier: bool | None = None
    heater: bool | None = None
    target_temp_c: float | None = None
    target_humidity_rh: float | None = None
    sun_exposure: str | None = None
    rain_protection: str | None = None
    pot_type: str | None = None
    substrate_type: str | None = None
    has_environment_sensors: bool = False
    notes: str | None = None


GrowStatus = Literal["active", "harvested", "abandoned"]

# Campos do GrowUpdate que nao aceitam null: colunas NOT NULL e a potencia do exaustor
# (registrada uma vez, nao volta a vazio). Os demais aceitam null para apagar o valor
# ("Nao sei" nos booleanos). Campo ausente no corpo nunca muda nada.
NON_NULLABLE_ON_UPDATE = frozenset({"name", "grow_type", "status", "has_environment_sensors", "exhaust_power_pct"})


class GrowUpdate(_LocationFields):
    name: str | None = None
    grow_type: str | None = None
    # abandoned arquiva o ambiente com as plantas ativas; active restaura (mesma regra de
    # DELETE /grows/{id} e POST /grows/{id}/restore)
    status: GrowStatus | None = None
    end_date: date | None = None
    tent_width_cm: int | None = None
    tent_depth_cm: int | None = None
    tent_height_cm: int | None = None
    lighting_watts: int | None = None
    light_type: str | None = None
    light_distance_cm: int | None = None
    # Derivado pelo servidor; clientes antigos podem enviar "N/M"
    photoperiod_hours: str | None = None
    photoperiod_on_at: time | None = None
    photoperiod_light_minutes: int | None = Field(
        default=None, ge=1, le=1440,
        description="Duracao da luz em minutos (1-1440). O servidor deriva photoperiod_hours. "
                    "Exige photoperiod_on_at (no corpo ou ja salvo), exceto 1440 (24/0). null volta a 'nao informado'.",
    )
    light_leak_controlled: bool | None = None
    exhaust_type: str | None = None
    exhaust_cfm: int | None = None
    exhaust_location: str | None = None
    exhaust_power_pct: int | None = Field(
        default=None, ge=0, le=100, multiple_of=5,
        description=(
            "Potencia do exaustor ajustada no controlador fisico (0-100, passos de 5). "
            "Somente registro: nao comanda o equipamento (nao ha MQTT de saida). "
            "Quando muda, o servidor grava exhaust_power_updated_at e um registro no historico do ambiente."
        ),
    )
    carbon_filter: bool | None = None
    intake_type: str | None = None
    internal_circulation_fans: int | None = None
    negative_pressure: bool | None = None
    fans: list[dict[str, Any]] | None = None
    air_conditioning: bool | None = None
    dehumidifier: bool | None = None
    humidifier: bool | None = None
    heater: bool | None = None
    target_temp_c: float | None = None
    target_humidity_rh: float | None = None
    sun_exposure: str | None = None
    rain_protection: str | None = None
    pot_type: str | None = None
    substrate_type: str | None = None
    has_environment_sensors: bool | None = None
    notes: str | None = None

    @model_validator(mode="after")
    def _required_fields_are_not_cleared(self):
        # PATCH grava os nulos enviados de proposito (exclude_unset). Estes campos nao podem
        # ficar vazios: recusar e mais seguro do que ignorar o null em silencio.
        cleared = sorted(f for f in NON_NULLABLE_ON_UPDATE if f in self.model_fields_set and getattr(self, f) is None)
        if cleared:
            raise ValueError(f"Estes campos não podem ficar vazios: {', '.join(cleared)}")
        return self


class EnvironmentMeasurement(BaseModel):
    value: float | None = None
    unit: str
    source: Literal["manual", "sensor"] | None = None
    source_id: uuid.UUID | None = None
    # Which record source_id points to, so the app can open it.
    source_kind: Literal["sensor_reading", "event", "environment_reading"] | None = None
    observed_at: datetime | None = None


class GrowEnvSnapshot(BaseModel):
    temperature: EnvironmentMeasurement = Field(default_factory=lambda: EnvironmentMeasurement(unit="°C"))
    humidity: EnvironmentMeasurement = Field(default_factory=lambda: EnvironmentMeasurement(unit="%"))
    light: EnvironmentMeasurement = Field(default_factory=lambda: EnvironmentMeasurement(unit="lux"))
    vpd: EnvironmentMeasurement = Field(default_factory=lambda: EnvironmentMeasurement(unit="kPa"))
    # Compatibility for older mobile versions. Use the per-indicator objects above.
    temp_c: float | None = None
    humidity_rh: float | None = None
    vpd_kpa: float | None = None
    lux: int | None = None
    recorded_at: datetime | None = None
    last_event_at: datetime | None = None
    last_watering_at: datetime | None = None


class GrowSummary(BaseModel):
    id: uuid.UUID
    name: str
    grow_type: str
    status: str
    start_date: date
    archived_at: datetime | None = None
    photoperiod_hours: str | None = None
    photoperiod_light_minutes: int | None = None
    # Local do ambiente: sem lat/lon o Inicio mostra "Defina onde fica" (sem clima)
    lat: float | None = None
    lon: float | None = None
    location_label: str | None = None
    pot_count: int = 0
    active_plant_count: int
    days_running: int
    env: GrowEnvSnapshot | None = None

    model_config = {"from_attributes": True}


class GrowResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    grow_type: str
    status: str
    start_date: date
    end_date: date | None
    archived_at: datetime | None = None
    tent_width_cm: int | None
    tent_depth_cm: int | None
    tent_height_cm: int | None
    lighting_watts: int | None
    light_type: str | None
    light_distance_cm: int | None
    photoperiod_hours: str | None
    photoperiod_on_at: time | None
    photoperiod_light_minutes: int | None = None
    light_leak_controlled: bool | None
    exhaust_type: str | None
    exhaust_cfm: int | None
    exhaust_location: str | None
    # Registro da potencia ajustada no controlador (nao e comando); horario gravado pelo servidor
    exhaust_power_pct: int | None = None
    exhaust_power_updated_at: datetime | None = None
    carbon_filter: bool | None
    intake_type: str | None
    internal_circulation_fans: int | None
    negative_pressure: bool | None
    fans: list[dict[str, Any]] | None
    air_conditioning: bool | None
    dehumidifier: bool | None
    humidifier: bool | None
    heater: bool | None
    target_temp_c: float | None
    target_humidity_rh: float | None
    lat: float | None
    lon: float | None
    location_label: str | None = None
    sun_exposure: str | None
    rain_protection: str | None
    pot_type: str | None
    substrate_type: str | None
    has_environment_sensors: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime
    env: GrowEnvSnapshot | None = None

    model_config = {"from_attributes": True}


class GrowPlantBrief(BaseModel):
    id: uuid.UUID
    display_name: str | None = None
    strain_name: str
    current_phase: str
    is_active: bool
    germination_date: date | None = None

    model_config = {"from_attributes": True}


class GrowDetailResponse(GrowResponse):
    plants: list[GrowPlantBrief] = []


class GrowArchiveResult(BaseModel):
    """Resposta de DELETE /grows/{id} (arquivar) e POST /grows/{id}/restore."""
    message: str
    grow_id: uuid.UUID
    status: str
    archived_at: datetime | None
    # Plantas arquivadas junto (DELETE) ou restauradas junto (restore)
    plants_affected: int
