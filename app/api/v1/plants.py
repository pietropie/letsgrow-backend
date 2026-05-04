import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.grow import Grow
from app.models.plant import Plant
from app.models.pot import Pot
from app.models.user import User
from app.schemas.plant import PlantCreate, PlantResponse, PlantUpdate
from app.services.auth import get_current_user

router = APIRouter()


async def _get_pot_owned_by(
    pot_id: uuid.UUID, grow_id: uuid.UUID, user_id: uuid.UUID, db: AsyncSession
) -> Pot:
    result = await db.execute(
        select(Pot)
        .join(Grow, Pot.grow_id == Grow.id)
        .where(Pot.id == pot_id, Pot.grow_id == grow_id, Grow.user_id == user_id)
    )
    pot = result.scalar_one_or_none()
    if not pot:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vaso não encontrado")
    return pot


@router.post("/{grow_id}/pots/{pot_id}/plant", response_model=PlantResponse, status_code=status.HTTP_201_CREATED)
async def create_plant(
    grow_id: uuid.UUID,
    pot_id: uuid.UUID,
    body: PlantCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    pot = await _get_pot_owned_by(pot_id, grow_id, current_user.id, db)

    existing = await db.execute(select(Plant).where(Plant.pot_id == pot.id))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Vaso já possui uma planta")

    plant = Plant(pot_id=pot.id, **body.model_dump())
    db.add(plant)
    await db.commit()
    await db.refresh(plant)
    return plant


@router.patch("/{grow_id}/pots/{pot_id}/plant", response_model=PlantResponse)
async def update_plant(
    grow_id: uuid.UUID,
    pot_id: uuid.UUID,
    body: PlantUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    pot = await _get_pot_owned_by(pot_id, grow_id, current_user.id, db)

    result = await db.execute(select(Plant).where(Plant.pot_id == pot.id))
    plant = result.scalar_one_or_none()
    if not plant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Planta não encontrada")

    for field, value in body.model_dump(exclude_none=True).items():
        setattr(plant, field, value)
    await db.commit()
    await db.refresh(plant)
    return plant
