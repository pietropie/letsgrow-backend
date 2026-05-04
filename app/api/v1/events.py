import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.event import GrowEvent
from app.models.plant import Plant
from app.models.pot import Pot
from app.models.grow import Grow
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.event import EventCreate, EventResponse, EventUpdate
from app.services.auth import get_current_user

router = APIRouter()


async def _get_plant_or_404(plant_id: uuid.UUID, user_id: uuid.UUID, db: AsyncSession) -> Plant:
    result = await db.execute(
        select(Plant)
        .join(Pot, Plant.pot_id == Pot.id)
        .join(Grow, Pot.grow_id == Grow.id)
        .where(Plant.id == plant_id, Grow.user_id == user_id)
    )
    plant = result.scalar_one_or_none()
    if not plant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Planta não encontrada")
    return plant


@router.get("/{plant_id}/events", response_model=list[EventResponse])
async def list_events(
    plant_id: uuid.UUID,
    limit: int = 50,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_plant_or_404(plant_id, current_user.id, db)
    result = await db.execute(
        select(GrowEvent)
        .where(GrowEvent.plant_id == plant_id)
        .order_by(GrowEvent.event_date.desc())
        .limit(limit)
    )
    return result.scalars().all()


@router.post("/{plant_id}/events", response_model=EventResponse, status_code=status.HTTP_201_CREATED)
async def create_event(
    plant_id: uuid.UUID,
    body: EventCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    plant = await _get_plant_or_404(plant_id, current_user.id, db)

    event = GrowEvent(plant_id=plant_id, **body.model_dump())
    db.add(event)

    # Auto-update plant phase on flip event
    if body.event_type == "flip" and plant.current_phase == "veg":
        plant.current_phase = "flower"
        plant.flip_date = body.event_date.date()
    elif body.event_type == "harvest":
        plant.current_phase = "done"
        plant.harvest_date = body.event_date.date()
        plant.is_active = False

    await db.commit()
    await db.refresh(event)
    return event


@router.patch("/{plant_id}/events/{event_id}", response_model=EventResponse)
async def update_event(
    plant_id: uuid.UUID,
    event_id: uuid.UUID,
    body: EventUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_plant_or_404(plant_id, current_user.id, db)
    result = await db.execute(
        select(GrowEvent).where(GrowEvent.id == event_id, GrowEvent.plant_id == plant_id)
    )
    event = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evento não encontrado")

    for field, value in body.model_dump(exclude_none=True).items():
        setattr(event, field, value)
    await db.commit()
    await db.refresh(event)
    return event


@router.delete("/{plant_id}/events/{event_id}", response_model=MessageResponse)
async def delete_event(
    plant_id: uuid.UUID,
    event_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_plant_or_404(plant_id, current_user.id, db)
    result = await db.execute(
        select(GrowEvent).where(GrowEvent.id == event_id, GrowEvent.plant_id == plant_id)
    )
    event = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evento não encontrado")

    await db.delete(event)
    await db.commit()
    return MessageResponse(message="Evento removido")
