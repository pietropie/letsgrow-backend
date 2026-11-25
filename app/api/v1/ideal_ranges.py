"""
Faixas ideais do ambiente (UX item 28, decisão 4). Montadas em /grows pelo grows.py.

GET    /grows/{grow_id}/ideal-ranges            Faixa que vale para cada medida (ajustada ou sugerida)
PUT    /grows/{grow_id}/ideal-ranges/{metric}   Ajusta a faixa no ambiente (vale para todas as fases)
DELETE /grows/{grow_id}/ideal-ranges/{metric}   Volta à sugestão pela fase
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.grow import Grow, GrowIdealRange
from app.models.user import User
from app.schemas.sensor_panel import AdjustableMetric, IdealRangeIn, IdealRangesOut
from app.services import ideal_ranges as ranges
from app.services import sensor_catalog as catalog
from app.services.auth import get_current_user
from app.services.sensor_panel import custom_ranges, grow_phase, range_out

router = APIRouter()


async def _grow(grow_id: uuid.UUID, user: User, db: AsyncSession) -> Grow:
    grow = (await db.execute(select(Grow).where(Grow.id == grow_id, Grow.user_id == user.id))).scalar_one_or_none()
    if grow is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ambiente não encontrado")
    return grow


async def ranges_view(db: AsyncSession, grow: Grow) -> dict:
    custom = await custom_ranges(db, grow.id)
    phase, basis = await grow_phase(db, grow.id)
    items = []
    for metric in ranges.RANGE_METRICS:
        meta = catalog.METRICS[metric]
        band = ranges.effective(metric, phase, custom)
        suggestion = ranges.effective(metric, phase, {})
        items.append({
            "metric": metric, "label": meta.label, "unit": meta.unit,
            "min": band.min if band else None, "max": band.max if band else None,
            "source": band.source if band else None,
            "needs_review": bool(band and band.needs_review),
            "suggested": range_out(suggestion),
            "adjustable": metric in ranges.ADJUSTABLE,
        })
    return {"grow_id": grow.id, "phase": phase, "phase_basis": basis, "ranges": items}


@router.get("/{grow_id}/ideal-ranges", response_model=IdealRangesOut)
async def get_ideal_ranges(
    grow_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """EC não é ajustável à parte: acompanha o PPM (EC = PPM / 500)."""
    return await ranges_view(db, await _grow(grow_id, current_user, db))


@router.put("/{grow_id}/ideal-ranges/{metric}", response_model=IdealRangesOut)
async def put_ideal_range(
    grow_id: uuid.UUID,
    metric: AdjustableMetric,
    body: IdealRangeIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    grow = await _grow(grow_id, current_user, db)
    low, high = ranges.BOUNDS[metric]
    if body.min < low or body.max > high:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail=f"Faixa fora do aceito para {metric} ({low}–{high})")
    row = (await db.execute(
        select(GrowIdealRange).where(GrowIdealRange.grow_id == grow.id, GrowIdealRange.metric == metric)
    )).scalar_one_or_none()
    if row is None:
        row = GrowIdealRange(grow_id=grow.id, metric=metric, min_value=body.min, max_value=body.max)
        db.add(row)
    else:
        row.min_value, row.max_value = body.min, body.max
    await db.commit()
    return await ranges_view(db, grow)


@router.delete("/{grow_id}/ideal-ranges/{metric}", response_model=IdealRangesOut)
async def delete_ideal_range(
    grow_id: uuid.UUID,
    metric: AdjustableMetric,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    grow = await _grow(grow_id, current_user, db)
    await db.execute(delete(GrowIdealRange).where(GrowIdealRange.grow_id == grow.id, GrowIdealRange.metric == metric))
    await db.commit()
    return await ranges_view(db, grow)
