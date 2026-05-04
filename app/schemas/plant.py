import uuid
from datetime import date, datetime

from pydantic import BaseModel


class PlantCreate(BaseModel):
    strain_name: str
    strain_type: str = "photo"
    genetics: str | None = None
    seed_type: str = "feminized"
    germination_date: date | None = None
    current_phase: str = "germination"
    expected_harvest_days: int | None = None


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


class PlantResponse(BaseModel):
    id: uuid.UUID
    pot_id: uuid.UUID
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
    created_at: datetime
    updated_at: datetime

    model_config = {"from_attributes": True}
