import uuid
from datetime import date, datetime
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator
from app.schemas.grow import EnvironmentMeasurement

from app.schemas.event import EventResponse


class PlantCreate(BaseModel):
    display_name: str | None = Field(default=None, max_length=100)
    strain_name: str = Field(default="", max_length=100)
    strain_type: str = "unknown"
    # photo | auto
    genetics: str | None = None
    # indica | sativa | hybrid
    seed_type: str = "unknown"
    # feminized | regular | autoflower
    germination_date: date | None = None
    current_phase: str = "unknown"
    flip_date: date | None = None
    # germination | seedling | veg | flower | harvest | done
    expected_harvest_days: int | None = None
    grow_id: uuid.UUID
    grow_label: str | None = None       # legado; ignorado na criacao
    pot_label: str | None = None
    pot_volume_liters: float | None = None
    substrate: str | None = None

    @model_validator(mode="after")
    def validate_identity(self):
        self.display_name = (self.display_name or "").strip() or None
        self.strain_name = self.strain_name.strip()
        if not self.display_name and not self.strain_name:
            raise ValueError("Informe um nome para identificar a planta")
        return self


class PlantUpdate(BaseModel):
    display_name: str | None = Field(default=None, max_length=100)
    strain_name: str | None = Field(default=None, max_length=100)
    strain_type: str | None = None
    genetics: str | None = None
    seed_type: str | None = None
    germination_date: date | None = None
    current_phase: str | None = None
    flip_date: date | None = None
    harvest_date: date | None = None
    expected_harvest_days: int | None = None
    is_active: bool | None = None
    grow_id: uuid.UUID | None = None    # vínculo relacional com grows
    grow_label: str | None = None       # fallback texto livre (legado)
    pot_label: str | None = None
    pot_volume_liters: float | None = None
    substrate: str | None = None

    @field_validator("grow_id", "strain_name", "strain_type", "seed_type", "current_phase", "is_active")
    @classmethod
    def non_nullable_fields(cls, value):
        if value is None:
            raise ValueError("Este campo não pode ser nulo")
        return value.strip() if isinstance(value, str) else value

    @field_validator("display_name")
    @classmethod
    def trim_display_name(cls, value):
        return (value or "").strip() or None


class PlantResponse(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    display_name: str | None = None
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
    grow_id: uuid.UUID
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
    """Dica contextual do Bob — sempre derivada de compute_bob_steps."""
    scenario: str       # water_due | flip_soon | pre_harvest | fert_due | topping | training | defoliation | env_critical
    tip: str            # Texto pronto do motor de regras (voz do Bob)
    priority: str       # "info" | "warning" | "urgent"
    icon: str           # Emoji representando o cenário


class PlantSummary(BaseModel):
    """Resumo para o card da home: últimas leituras de ambiente, PPM e rega.

    Temperatura/umidade são compartilhadas entre plantas do mesmo grow.
    """
    plant_id: uuid.UUID
    temperature: EnvironmentMeasurement | None = None
    humidity: EnvironmentMeasurement | None = None
    last_temperature_c: float | None = None
    last_temperature_at: datetime | None = None
    last_humidity_rh: float | None = None
    last_humidity_at: datetime | None = None
    last_ppm: float | None = None
    last_watering_at: datetime | None = None
    last_watering_has_fert: bool = False
    # Imagem de capa da strain — usada como background do PlantCard no app
    strain_image_url: str | None = None
    # Início v3 (UX item 20): foto mais recente da planta e o que fazer a seguir
    latest_photo_url: str | None = None
    latest_photo_at: datetime | None = None
    latest_photo_event_id: uuid.UUID | None = None
    cover_image_url: str | None = None       # a foto mais recente; sem foto, a imagem da genética
    phase_day: int | None = None             # dia da fase atual (o primeiro é o dia 1), só com data registrada
    next_action: "PlantNextAction | None" = None
    last_event: "PlantLastEvent | None" = None
    # Plantas v2 (UX item 22): o que o cartão de arquivada ou colhida mostra
    event_count: int = 0                     # registros do diário da planta
    cycle_start: date | None = None          # germinação cadastrada
    cycle_end: date | None = None            # colheita; sem ela, o último registro (inativa) ou hoje (ativa)
    cycle_days: int | None = None            # de cycle_start a cycle_end, contando os dois dias


class PlantNextAction(BaseModel):
    """O primeiro item de atenção da planta (mesma fonte de "Precisa de atenção")."""
    action: str                              # watering | temperature | humidity | harvest | flush | ...
    tier: Literal["critical", "anomaly", "attention"]
    message: str
    evidence: list[dict] = []                # registros que sustentam o item (formato dos alertas do Bobby)


class PlantLastEvent(BaseModel):
    id: uuid.UUID
    event_type: str
    label: str                               # "Rega", "Nutrição"...
    event_date: datetime


class SuggestionBasis(BaseModel):
    kind: Literal["attention", "phase", "reading"]
    text: str                                # de onde veio, em uma linha
    tier: Literal["critical", "anomaly", "attention", "info"] | None = None
    evidence: list[dict] = []


class BobSuggestion(BaseModel):
    label: str                               # texto do botão
    prompt: str                              # pergunta pronta para o Bobby
    plant_id: uuid.UUID | None = None
    grow_id: uuid.UUID | None = None
    basis: SuggestionBasis


PlantSummary.model_rebuild()
