import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, model_validator

RangeState = Literal["ok", "near", "out", "none"]
SensorKind = Literal["air", "soil", "light", "solution", "relay", "hub", "co2", "ph", "root"]
AdjustableMetric = Literal["temp_air", "humidity_air", "vpd_kpa", "lux", "soil_moisture_pct", "ppm"]


class RangeOut(BaseModel):
    min: float
    max: float
    # custom = ajustada no ambiente | phase = sugerida pela fase
    source: Literal["custom", "phase"]
    phase: str | None
    needs_review: bool
    reference: str | None


class MetricOut(BaseModel):
    metric: str
    label: str
    unit: str
    decimals: int
    computed: bool
    note: str | None
    value: float | None
    range: RangeOut | None
    state: RangeState
    out_of_range_since: datetime | None


class HistoryBucket(BaseModel):
    start: datetime
    avg: float | None
    min: float | None
    max: float | None
    n: int
    state: RangeState


class HistoryOut(BaseModel):
    metric: str
    window: Literal["24h", "3d", "7d"]
    bucket_minutes: int
    buckets: list[HistoryBucket]
    min: float | None
    max: float | None


class SensorHistoryOut(HistoryOut):
    device_id: uuid.UUID
    unit: str
    range: RangeOut | None


class Placement(BaseModel):
    plant_id: uuid.UUID | None
    label: str | None


class SensorCard(BaseModel):
    device_id: uuid.UUID
    card_id: str
    name: str
    source: Literal["hub", "satellite"]
    sensor_kind: SensorKind | None
    kind_label: str | None
    tag: str | None
    description: str | None
    icon: str
    photo_url: str | None
    placement: Placement
    # live | no_signal (satélite parou) | stale (Hub offline, leitura antiga) | waiting (sem leitura)
    status: Literal["live", "no_signal", "stale", "waiting"]
    is_online: bool
    last_reading_at: datetime | None
    metrics: list[MetricOut]
    worst_state: RangeState
    history: HistoryOut | None


class HubCard(BaseModel):
    id: uuid.UUID
    name: str
    sensor_kind: Literal["hub"]
    kind_label: str
    tag: str
    description: str
    icon: str
    photo_url: str | None
    online: bool
    last_seen_at: datetime | None
    satellites_total: int
    satellites_online: int
    clock: str | None


class SensorPanelOut(BaseModel):
    grow_id: uuid.UUID
    phase: str
    # plants = fase da maioria das plantas | assumed = nenhuma tem fase (vale o vegetativo) | none = sem plantas
    phase_basis: Literal["plants", "assumed", "none"]
    light_on: bool | None
    hub: HubCard | None
    cards: list[SensorCard]
    out_of_range_count: int
    generated_at: datetime


class IdealRangeOut(BaseModel):
    metric: str
    label: str
    unit: str
    min: float | None
    max: float | None
    source: Literal["custom", "phase"] | None
    needs_review: bool
    suggested: RangeOut | None
    adjustable: bool


class IdealRangesOut(BaseModel):
    grow_id: uuid.UUID
    phase: str
    phase_basis: Literal["plants", "assumed", "none"]
    ranges: list[IdealRangeOut]


class IdealRangeIn(BaseModel):
    min: float
    max: float

    @model_validator(mode="after")
    def _order(self):
        if not self.min < self.max:
            raise ValueError("min precisa ser menor que max")
        return self


class SensorModelOut(BaseModel):
    key: str
    name: str
    sensor_kind: str | None
    photo_url: str | None
    updated_at: datetime | None


class SensorModelPut(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=80)
    sensor_kind: SensorKind | None = None
    # null ou "" apaga a foto (o app volta ao ícone)
    photo_url: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def _url(self):
        if self.photo_url and not self.photo_url.startswith("https://"):
            raise ValueError("photo_url precisa ser https://")
        return self
