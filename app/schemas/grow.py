import uuid
from datetime import date, datetime, time
from typing import Any, Literal

from pydantic import BaseModel, Field

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


class GrowCreate(BaseModel):
    name: str
    grow_type: str = "indoor"
    start_date: date = Field(default_factory=date.today)
    tent_width_cm: int | None = None
    tent_depth_cm: int | None = None
    tent_height_cm: int | None = None
    lighting_watts: int | None = None
    light_type: str | None = None
    light_distance_cm: int | None = None
    photoperiod_hours: str | None = None
    photoperiod_on_at: time | None = None
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
    lat: float | None = None
    lon: float | None = None
    sun_exposure: str | None = None
    rain_protection: str | None = None
    pot_type: str | None = None
    substrate_type: str | None = None
    has_environment_sensors: bool = False
    notes: str | None = None


class GrowUpdate(BaseModel):
    name: str | None = None
    grow_type: str | None = None
    status: str | None = None
    end_date: date | None = None
    tent_width_cm: int | None = None
    tent_depth_cm: int | None = None
    tent_height_cm: int | None = None
    lighting_watts: int | None = None
    light_type: str | None = None
    light_distance_cm: int | None = None
    photoperiod_hours: str | None = None
    photoperiod_on_at: time | None = None
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
    lat: float | None = None
    lon: float | None = None
    sun_exposure: str | None = None
    rain_protection: str | None = None
    pot_type: str | None = None
    substrate_type: str | None = None
    has_environment_sensors: bool | None = None
    notes: str | None = None


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
    photoperiod_hours: str | None = None
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
    tent_width_cm: int | None
    tent_depth_cm: int | None
    tent_height_cm: int | None
    lighting_watts: int | None
    light_type: str | None
    light_distance_cm: int | None
    photoperiod_hours: str | None
    photoperiod_on_at: time | None
    light_leak_controlled: bool | None
    exhaust_type: str | None
    exhaust_cfm: int | None
    exhaust_location: str | None
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
