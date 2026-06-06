import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.grow import Grow
from app.models.plant import Plant
from app.models.pot import Pot
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.pot import PotCreate, PotDetailResponse, PotResponse, PotUpdate
from app.services.auth import get_current_user
from app.services.subscription import check_pot_limit

router = APIRouter()


async def _get_grow_or_404(grow_id: uuid.UUID, user_id: uuid.UUID, db: AsyncSession) -> Grow:
    result = await db.execute(
        select(Grow).where(Grow.id == grow_id, Grow.user_id == user_id)
    )
    grow = result.scalar_one_or_none()
    if not grow:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grow não encontrado")
    return grow


async def _get_pot_or_404(pot_id: uuid.UUID, grow_id: uuid.UUID, db: AsyncSession) -> Pot:
    result = await db.execute(
        select(Pot).where(Pot.id == pot_id, Pot.grow_id == grow_id)
    )
    pot = result.scalar_one_or_none()
    if not pot:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vaso não encontrado")
    return pot


@router.get("/{grow_id}/pots", response_model=list[PotDetailResponse])
async def list_pots(
    grow_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_grow_or_404(grow_id, current_user.id, db)
    result = await db.execute(
        select(Pot)
        .options(selectinload(Pot.plant))
        .where(Pot.grow_id == grow_id)
        .order_by(Pot.position)
    )
    return result.scalars().all()


@router.post("/{grow_id}/pots", response_model=PotResponse, status_code=status.HTTP_201_CREATED)
async def create_pot(
    grow_id: uuid.UUID,
    body: PotCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_grow_or_404(grow_id, current_user.id, db)

    count_result = await db.execute(select(func.count(Pot.id)).where(Pot.grow_id == grow_id))
    current_count = count_result.scalar() or 0
    check_pot_limit(current_user, current_count)

    pot = Pot(grow_id=grow_id, **body.model_dump())
    db.add(pot)
    await db.commit()
    await db.refresh(pot)
    return pot


@router.get("/{grow_id}/pots/{pot_id}", response_model=PotDetailResponse)
async def get_pot(
    grow_id: uuid.UUID,
    pot_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_grow_or_404(grow_id, current_user.id, db)
    result = await db.execute(
        select(Pot).options(selectinload(Pot.plant)).where(Pot.id == pot_id, Pot.grow_id == grow_id)
    )
    pot = result.scalar_one_or_none()
    if not pot:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Vaso não encontrado")
    return pot


@router.patch("/{grow_id}/pots/{pot_id}", response_model=PotResponse)
async def update_pot(
    grow_id: uuid.UUID,
    pot_id: uuid.UUID,
    body: PotUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_grow_or_404(grow_id, current_user.id, db)
    pot = await _get_pot_or_404(pot_id, grow_id, db)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(pot, field, value)
    await db.commit()
    await db.refresh(pot)
    return pot


@router.delete("/{grow_id}/pots/{pot_id}", response_model=MessageResponse)
async def delete_pot(
    grow_id: uuid.UUID,
    pot_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await _get_grow_or_404(grow_id, current_user.id, db)
    pot = await _get_pot_or_404(pot_id, grow_id, db)
    await db.delete(pot)
    await db.commit()
    return MessageResponse(message="Vaso removido com sucesso")
