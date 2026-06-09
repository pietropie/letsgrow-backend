import uuid
from datetime import date, datetime
from typing import Literal

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


class GrowCreate(BaseModel):
    name: str
    grow_type: str = "indoor"
    start_date: date = Field(default_factory=date.today)
    # Dimensões da tenda
    tent_width_cm: int | None = None
    tent_depth_cm: int | None = None
    tent_height_cm: int | None = None
    # Iluminação
    lighting_watts: int | None = None
    light_type: str | None = None
    light_distance_cm: int | None = None
    photoperiod_hours: str | None = None
    light_leak_controlled: bool | None = None
    # Ventilação
    exhaust_type: str | None = None
    carbon_filter: bool | None = None
    intake_type: str | None = None
    internal_circulation_fans: int | None = None
    negative_pressure: bool | None = None
    # Controle climático
    air_conditioning: bool | None = None
    dehumidifier: bool | None = None
    humidifier: bool | None = None
    heater: bool | None = None
    # Substrato
    pot_type: str | None = None
    substrate_type: str | None = None
    # Sensores
    has_environment_sensors: bool = False
    notes: str | None = None


class GrowUpdate(BaseModel):
    name: str | None = None
    grow_type: str | None = None
    status: str | None = None
    end_date: date | None = None
    # Dimensões da tenda
    tent_width_cm: int | None = None
    tent_depth_cm: int | None = None
    tent_height_cm: int | None = None
    # Iluminação
    lighting_watts: int | None = None
    light_type: str | None = None
    light_distance_cm: int | None = None
    photoperiod_hours: str | None = None
    light_leak_controlled: bool | None = None
    # Ventilação
    exhaust_type: str | None = None
    carbon_filter: bool | None = None
    intake_type: str | None = None
    internal_circulation_fans: int | None = None
    negative_pressure: bool | None = None
    # Controle climático
    air_conditioning: bool | None = None
    dehumidifier: bool | None = None
    humidifier: bool | None = None
    heater: bool | None = None
    # Substrato
    pot_type: str | None = None
    substrate_type: str | None = None
    # Sensores
    has_environment_sensors: bool | None = None
    notes: str | None = None


class GrowSummary(BaseModel):
    id: uuid.UUID
    name: str
    grow_type: str
    status: str
    start_date: date
    pot_count: int
    active_plant_count: int
    days_running: int

    model_config = {"from_attributes": True}


class GrowResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    name: str
    grow_type: str
    status: str
    start_date: date
    end_date: date | None
    # Dimensões da tenda
    tent_width_cm: int | None
    tent_depth_cm: int | None
    tent_height_cm: int | None
    # Iluminação
    lighting_watts: int | None
    light_type: str | None
    light_distance_cm: int | None
    photoperiod_hours: str | None
    light_leak_controlled: bool | None
    # Ventilação
    exhaust_type: str | None
    carbon_filter: bool | None
    intake_type: str | None
    internal_circulation_fans: int | None
    negative_pressure: bool | None
    # Controle climático
    air_conditioning: bool | None
    dehumidifier: bool | None
    humidifier: bool | None
    heater: bool | None
    # Substrato
    pot_type: str | None
    substrate_type: str | None
    # Sensores
    has_environment_sensors: bool
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
