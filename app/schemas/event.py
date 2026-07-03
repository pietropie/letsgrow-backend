import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class EventCreate(BaseModel):
    event_type: str
    event_date: datetime

    # Rega: entrada
    ppm: float | None = None
    ph_in: float | None = None
    water_volume_ml: float | None = None

    # Rega: saida (runoff)
    ph_out: float | None = None
    ec_out: float | None = None
    has_runoff: bool | None = None

    # Nutricao: "base" | "booster" | "suplemento" | "foliar"
    nutrient_subtype: str | None = None
    is_flush: bool | None = None

    # Treinamento: "topping" | "fim" | "lst" | "supercropping" | "lollipopping" | "schwazzing"
    training_subtype: str | None = None

    # Tricomas (event_type = "trichome_reading")
    trichome_clear_pct: int | None = None
    trichome_milky_pct: int | None = None
    trichome_amber_pct: int | None = None

    # Ambiente e medicoes fisicas
    temperature_c: float | None = None
    humidity_rh: float | None = None
    weight_g: float | None = None

    # Diagnostico: "leve" | "moderado" | "grave"
    severity: str | None = None

    notes: str | None = None
    photo_keys: list[str] | None = None

    # Catch-all: {"harvest_method": "wet_trim", "symptom_type": "amarelamento",
    #             "symptom_location": "folhas velhas", "soil_wet": true, "node_number": 4}
    metadata: dict[str, Any] | None = None


class EventUpdate(BaseModel):
    event_type: str | None = None
    event_date: datetime | None = None

    ppm: float | None = None
    ph_in: float | None = None
    water_volume_ml: float | None = None

    ph_out: float | None = None
    ec_out: float | None = None
    has_runoff: bool | None = None

    nutrient_subtype: str | None = None
    is_flush: bool | None = None

    training_subtype: str | None = None

    trichome_clear_pct: int | None = None
    trichome_milky_pct: int | None = None
    trichome_amber_pct: int | None = None

    temperature_c: float | None = None
    humidity_rh: float | None = None
    weight_g: float | None = None

    severity: str | None = None

    notes: str | None = None
    photo_keys: list[str] | None = None
    metadata: dict[str, Any] | None = None


class EventAnalysisResponse(BaseModel):
    event_id: uuid.UUID
    status: str
    resumo: str
    problemas: list[str]
    recomendacoes: list[str]
    observacao_foto: str | None
    photos_analyzed: int


class EventResponse(BaseModel):
    id: uuid.UUID
    plant_id: uuid.UUID
    event_type: str
    event_date: datetime

    ppm: float | None
    ph_in: float | None
    water_volume_ml: float | None

    ph_out: float | None
    ec_out: float | None
    has_runoff: bool | None

    nutrient_subtype: str | None
    is_flush: bool | None

    training_subtype: str | None

    trichome_clear_pct: int | None
    trichome_milky_pct: int | None
    trichome_amber_pct: int | None

    temperature_c: float | None
    humidity_rh: float | None
    weight_g: float | None

    severity: str | None

    notes: str | None
    photo_keys: list[str] | None

    # "metadata" e reservado pelo SQLAlchemy Declarative API.
    # Atributo ORM = event_metadata; coluna DB = "metadata".
    # serialization_alias garante que o JSON de resposta continue usando "metadata".
    event_metadata: dict[str, Any] | None = Field(None, serialization_alias="metadata")

    created_at: datetime

    model_config = {"from_attributes": True, "populate_by_name": True}
