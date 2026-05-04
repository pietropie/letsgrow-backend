import uuid
from datetime import date, datetime

from pydantic import BaseModel


class GrowCreate(BaseModel):
    name: str
    grow_type: str = "indoor"
    start_date: date
    tent_width_cm: int | None = None
    tent_depth_cm: int | None = None
    tent_height_cm: int | None = None
    lighting_watts: int | None = None
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
    tent_width_cm: int | None
    tent_depth_cm: int | None
    tent_height_cm: int | None
    lighting_watts: int | None
    notes: str | None
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
