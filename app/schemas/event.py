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
    notes: str | None = None
    photo_keys: list[str] | None = None


class EventUpdate(BaseModel):
    event_type: str | None = None
    event_date: datetime | None = None
    ppm: float | None = None
    ph_in: float | None = None
    ph_out: float | None = None
    water_volume_ml: float | None = None
    notes: str | None = None
    photo_keys: list[str] | None = None


class EventResponse(BaseModel):
    id: uuid.UUID
    plant_id: uuid.UUID
    event_type: str
    event_date: datetime
    ppm: float | None
    ph_in: float | None
    ph_out: float | None
    water_volume_ml: float | None
    notes: str | None
    photo_keys: list[str] | None
    created_at: datetime

    model_config = {"from_attributes": True}
