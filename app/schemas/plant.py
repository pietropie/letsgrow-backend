import uuid
from datetime import date, datetime

from pydantic import BaseModel

from app.schemas.event import EventResponse


class PlantCreate(BaseModel):
    strain_name: str
    strain_type: str = "photo"
    # photo | auto
    genetics: str | None = None
    # indica | sativa | hybrid
    seed_type: str = "feminized"
    # feminized | regular | autoflower
    germination_date: date | None = None
    current_phase: str = "germination"
    # germination | seedling | veg | flower | harvest | done
    expected_harvest_days: int | None = None
    grow_label: str | None = None
    pot_label: str | None = None
    pot_volume_liters: float | None = None
    substrate: str | None = None


class PlantUpdate(BaseModel):
    strain_name: str | None = None
    strain_type: str | None = None
    genetics: str | None = None
    seed_type: str | None = None
    germination_date: date | None = None
    current_phase: str | None = None
    flip_date: date | None = None
    harvest_date: date | None = None
    expected_harvest_days: int | None = None
    is_active: bool | None = None
    grow_label: str | None = None
    pot_label: str | None = None
    pot_volume_liters: float | None = None
    substrate: str | None = None


class PlantResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    strain_name: str
    strain_type: str
    genetics: str | None
    seed_type: str
    germination_date: date | None
    current_phase: str
    flip_date: date | None
    harvest_date: date | None
    expected_harvest_days: int | None
    is_active: bool
    grow_label: str | None
    pot_label: str | None
    pot_volume_liters: float | None
    substrate: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}


class PlantDetailResponse(PlantResponse):
    """PlantResponse com lista de eventos embutida — usada em GET /plants/{plant_id}."""
    events: list[EventResponse] = []


class BobTipResponse(BaseModel):
    """Dica contextual do Bob para a planta — gerada por regras + LLM."""
    scenario: str       # "flip_soon" | "water_due" | "fert_due" | "pre_harvest" | "seedling"
    tip: str            # Texto em linguagem natural no estilo Bob
    priority: str       # "info" | "warning" | "urgent"
    icon: str           # Emoji representando o cenário


class PlantSummary(BaseModel):
    """Resumo para o card da home: últimas leituras de ambiente, PPM e rega.

    Temperatura/umidade são compartilhadas entre plantas do mesmo `grow_label`
    (ver lógica em app/api/v1/plants.py::_compute_plant_summary).
    """
    plant_id: uuid.UUID
    last_temperature_c: float | None = None
    last_temperature_at: datetime | None = None
    last_humidity_rh: float | None = None
    last_humidity_at: datetime | None = None
    last_ppm: float | None = None
    last_watering_at: datetime | None = None
    last_watering_has_fert: bool = False
