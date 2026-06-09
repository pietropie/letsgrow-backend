import uuid
from datetime import date

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.grow import Grow
from app.models.plant import Plant
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.grow import GrowCreate, GrowResponse, GrowSummary, GrowUpdate
from app.services.auth import get_current_user
from app.services.subscription import check_grow_limit

router = APIRouter()


async def _get_grow_or_404(grow_id: uuid.UUID, user_id: uuid.UUID, db: AsyncSession) -> Grow:
    result = await db.execute(
        select(Grow).where(Grow.id == grow_id, Grow.user_id == user_id)
    )
    grow = result.scalar_one_or_none()
    if not grow:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grow não encontrado")
    return grow


@router.get("", response_model=list[GrowSummary])
async def list_grows(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Grow).where(Grow.user_id == current_user.id).order_by(Grow.created_at.desc())
    )
    grows = result.scalars().all()

    summaries = []
    for grow in grows:
        active_plant_result = await db.execute(
            select(func.count(Plant.id))
            .where(Plant.grow_id == grow.id, Plant.is_active == True)
        )
        active_count = active_plant_result.scalar() or 0

        summaries.append(
            GrowSummary(
                id=grow.id,
                name=grow.name,
                grow_type=grow.grow_type,
                status=grow.status,
                start_date=grow.start_date,
                pot_count=0,
                active_plant_count=active_count,
                days_running=(date.today() - grow.start_date).days,
            )
        )
    return summaries


@router.post("", response_model=GrowResponse, status_code=status.HTTP_201_CREATED)
async def create_grow(
    body: GrowCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    count_result = await db.execute(
        select(func.count(Grow.id)).where(Grow.user_id == current_user.id, Grow.status == "active")
    )
    current_count = count_result.scalar() or 0
    check_grow_limit(current_user, current_count)

    grow = Grow(user_id=current_user.id, **body.model_dump())
    db.add(grow)
    await db.commit()
    await db.refresh(grow)
    return grow


@router.get("/{grow_id}", response_model=GrowResponse)
async def get_grow(
    grow_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return await _get_grow_or_404(grow_id, current_user.id, db)


@router.patch("/{grow_id}", response_model=GrowResponse)
async def update_grow(
    grow_id: uuid.UUID,
    body: GrowUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    grow = await _get_grow_or_404(grow_id, current_user.id, db)
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(grow, field, value)
    await db.commit()
    await db.refresh(grow)
    return grow


@router.delete("/{grow_id}", response_model=MessageResponse)
async def delete_grow(
    grow_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    grow = await _get_grow_or_404(grow_id, current_user.id, db)
    grow.status = "abandoned"
    await db.commit()
    return MessageResponse(message="Grow arquivado com sucesso")
