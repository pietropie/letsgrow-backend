import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.event import GrowEvent
from app.models.plant import Plant
from app.models.user import User
from app.schemas.event import EventCreate, EventResponse, EventUpdate
from app.schemas.plant import PlantCreate, PlantDetailResponse, PlantResponse, PlantUpdate
from app.schemas.common import MessageResponse
from app.services.auth import get_current_user

router = APIRouter()


async def _get_plant_or_404(plant_id: uuid.UUID, user_id: uuid.UUID, db: AsyncSession) -> Plant:
    result = await db.execute(
        select(Plant).where(Plant.id == plant_id, Plant.user_id == user_id)
    )
    plant = result.scalar_one_or_none()
    if not plant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Planta não encontrada")
    return plant


# ---------------------------------------------------------------------------
# GET /plants
# ---------------------------------------------------------------------------

@router.get("", response_model=list[PlantResponse])
async def list_plants(
    grow_label: Optional[str] = Query(default=None, description="Filtrar por grow_label"),
    is_active: Optional[bool] = Query(default=None, description="Filtrar por status ativo"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Lista todas as plantas do usuário autenticado."""
    stmt = select(Plant).where(Plant.user_id == current_user.id)
    if grow_label is not None:
        stmt = stmt.where(Plant.grow_label == grow_label)
    if is_active is not None:
        stmt = stmt.where(Plant.is_active == is_active)
    stmt = stmt.order_by(Plant.created_at.desc())
    result = await db.execute(stmt)
    return result.scalars().all()


# ---------------------------------------------------------------------------
# POST /plants
# ---------------------------------------------------------------------------

@router.post("", response_model=PlantResponse, status_code=status.HTTP_201_CREATED)
async def create_plant(
    body: PlantCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Cria uma nova planta vinculada diretamente ao usuário."""
    plant = Plant(user_id=current_user.id, **body.model_dump())
    db.add(plant)
    await db.commit()
    await db.refresh(plant)
    return plant


# ---------------------------------------------------------------------------
# GET /plants/{plant_id}
# ---------------------------------------------------------------------------

@router.get("/{plant_id}", response_model=PlantDetailResponse)
async def get_plant(
    plant_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Retorna detalhes da planta incluindo seus eventos."""
    result = await db.execute(
        select(Plant)
        .options(selectinload(Plant.events))
        .where(Plant.id == plant_id, Plant.user_id == current_user.id)
    )
    plant = result.scalar_one_or_none()
    if not plant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Planta não encontrada")
    return plant


# ---------------------------------------------------------------------------
# PATCH /plants/{plant_id}
# ---------------------------------------------------------------------------

@router.patch("/{plant_id}", response_model=PlantResponse)
async def update_plant(
    plant_id: uuid.UUID,
    body: PlantUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Atualiza campos da planta. Apenas campos enviados são alterados."""
    plant = await _get_plant_or_404(plant_id, current_user.id, db)
    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(plant, field, value)
    await db.commit()
    await db.refresh(plant)
    return plant


# ---------------------------------------------------------------------------
# DELETE /plants/{plant_id}  — soft delete
# ---------------------------------------------------------------------------

@router.delete("/{plant_id}", response_model=MessageResponse)
async def delete_plant(
    plant_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Soft delete: marca a planta como inativa (is_active = False)."""
    plant = await _get_plant_or_404(plant_id, current_user.id, db)
    plant.is_active = False
    await db.commit()
    return MessageResponse(message="Planta desativada")


# ---------------------------------------------------------------------------
# GET /plants/{plant_id}/events
# ---------------------------------------------------------------------------

@router.get("/{plant_id}/events", response_model=list[EventResponse])
async def list_events(
    plant_id: uuid.UUID,
    limit: int = Query(default=50, ge=1, le=500),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Lista os eventos de uma planta do usuário."""
    await _get_plant_or_404(plant_id, current_user.id, db)
    result = await db.execute(
        select(GrowEvent)
        .where(GrowEvent.plant_id == plant_id)
        .order_by(GrowEvent.event_date.desc())
        .limit(limit)
    )
    return result.scalars().all()


# ---------------------------------------------------------------------------
# POST /plants/{plant_id}/events
# ---------------------------------------------------------------------------

@router.post("/{plant_id}/events", response_model=EventResponse, status_code=status.HTTP_201_CREATED)
async def create_event(
    plant_id: uuid.UUID,
    body: EventCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Cria um evento para a planta e atualiza a fase automaticamente quando necessário."""
    plant = await _get_plant_or_404(plant_id, current_user.id, db)

    event = GrowEvent(plant_id=plant_id, **body.model_dump())
    db.add(event)

    # Auto-update plant phase on special event types
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


# ---------------------------------------------------------------------------
# PATCH /plants/{plant_id}/events/{event_id}
# ---------------------------------------------------------------------------

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

    for field, value in body.model_dump(exclude_unset=True).items():
        setattr(event, field, value)
    await db.commit()
    await db.refresh(event)
    return event


# ---------------------------------------------------------------------------
# DELETE /plants/{plant_id}/events/{event_id}
# ---------------------------------------------------------------------------

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
