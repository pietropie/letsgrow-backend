import uuid
from datetime import datetime

from pydantic import BaseModel


class EventCreate(BaseModel):
    event_type: str
    event_date: datetime
    ppm: float | None = None
    ph_in: float | None = None
    ph_out: float | None = None
    water_volume_ml: float | None = None
    # MVP fields recommended by cultivation consultant
    temperature_c: float | None = None
    humidity_rh: float | None = None
    weight_g: float | None = None
    severity: str | None = None
    is_flush: bool | None = None
    notes: str | None = None
    photo_keys: list[str] | None = None


class EventUpdate(BaseModel):
    event_type: str | None = None
    event_date: datetime | None = None
    ppm: float | None = None
    ph_in: float | None = None
    ph_out: float | None = None
    water_volume_ml: float | None = None
    # MVP fields recommended by cultivation consultant
    temperature_c: float | None = None
    humidity_rh: float | None = None
    weight_g: float | None = None
    severity: str | None = None
    is_flush: bool | None = None
    notes: str | None = None
    photo_keys: list[str] | None = None


class EventAnalysisResponse(BaseModel):
    event_id: uuid.UUID
    # Campos estruturados retornados pelo Bob (JSON do LLM)
    status: str                   # "saudavel" | "atencao" | "critico"
    resumo: str                   # 1-2 frases — exibidas no card de resumo
    problemas: list[str]          # lista de problemas detectados (pode ser vazia)
    recomendacoes: list[str]      # lista de recomendações práticas
    observacao_foto: str | None   # nota se qualidade das fotos limitou a análise
    photos_analyzed: int


class EventResponse(BaseModel):
    id: uuid.UUID
    plant_id: uuid.UUID
    event_type: str
    event_date: datetime
    ppm: float | None
    ph_in: float | None
    ph_out: float | None
    water_volume_ml: float | None
    # MVP fields recommended by cultivation consultant
    temperature_c: float | None
    humidity_rh: float | None
    weight_g: float | None
    severity: str | None
    is_flush: bool | None
    notes: str | None
    photo_keys: list[str] | None
    created_at: datetime

    model_config = {"from_attributes": True}
