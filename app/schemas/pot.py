import uuid
from datetime import datetime

from pydantic import BaseModel

from app.schemas.plant import PlantResponse


class PotCreate(BaseModel):
    label: str
    volume_liters: float = 7.0
    substrate: str | None = None
    position: int = 0


class PotUpdate(BaseModel):
    label: str | None = None
    volume_liters: float | None = None
    substrate: str | None = None
    position: int | None = None


class PotResponse(BaseModel):
    id: uuid.UUID
    grow_id: uuid.UUID
    label: str
    volume_liters: float
    substrate: str | None
    position: int

    model_config = {"from_attributes": True}


class PotDetailResponse(PotResponse):
    plant: PlantResponse | None = None
