import uuid
from datetime import date, datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.environment_reading import EnvironmentReading
from app.models.grow import Grow
from app.models.plant import Plant
from app.models.user import User
from app.schemas.common import MessageResponse
from app.schemas.grow import (
    GrowArchiveResult,
    GrowCreate,
    GrowDetailResponse,
    GrowPlantBrief,
    GrowResponse,
    GrowSummary,
    GrowUpdate,
)
from app.services.auth import get_current_user
from app.services.grow_env import latest_grow_env
from app.services.subscription import check_grow_limit, check_plant_limit

router = APIRouter()


async def _get_grow_or_404(grow_id: uuid.UUID, user_id: uuid.UUID, db: AsyncSession) -> Grow:
    result = await db.execute(
        select(Grow).where(Grow.id == grow_id, Grow.user_id == user_id)
    )
    grow = result.scalar_one_or_none()
    if not grow:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grow não encontrado")
    return grow


def _grow_response(grow: Grow, env=None) -> GrowResponse:
    data = GrowResponse.model_validate(grow)
    if env is not None:
        data.env = env
    return data


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
            select(func.count(Plant.id)).where(Plant.grow_id == grow.id, Plant.is_active == True)  # noqa: E712
        )
        active_count = active_plant_result.scalar() or 0
        env = await latest_grow_env(db, grow.id)

        summaries.append(
            GrowSummary(
                id=grow.id,
                name=grow.name,
                grow_type=grow.grow_type,
                status=grow.status,
                start_date=grow.start_date,
                archived_at=grow.archived_at,
                photoperiod_hours=grow.photoperiod_hours,
                pot_count=0,
                active_plant_count=active_count,
                days_running=(date.today() - grow.start_date).days,
                env=env,
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
    return _grow_response(grow)


@router.get("/{grow_id}", response_model=GrowDetailResponse)
async def get_grow(
    grow_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    grow = await _get_grow_or_404(grow_id, current_user.id, db)
    plants_result = await db.execute(
        select(Plant).where(Plant.grow_id == grow.id).order_by(Plant.created_at.desc())
    )
    plants = plants_result.scalars().all()
    env = await latest_grow_env(db, grow.id)
    # Validar via GrowResponse: GrowDetailResponse leria a relação lazy Grow.plants,
    # o que na AsyncSession dispara MissingGreenlet (HTTP 500).
    return GrowDetailResponse(
        **GrowResponse.model_validate(grow).model_dump(exclude={"env"}),
        env=env,
        plants=[GrowPlantBrief.model_validate(p) for p in plants],
    )


@router.patch("/{grow_id}", response_model=GrowResponse)
async def update_grow(
    grow_id: uuid.UUID,
    body: GrowUpdate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Atualiza o ambiente. So os campos enviados mudam.

    Enviar `null` apaga um campo opcional (por exemplo "Nao sei" num booleano). `name`,
    `grow_type`, `status`, `has_environment_sensors` e `exhaust_power_pct` nao aceitam null (422).
    `fans: []` registra que nao ha ventiladores; `fans: null` volta a "nao informado".

    `exhaust_power_pct` e so o registro da potencia ajustada no controlador do exaustor:
    nada e enviado ao equipamento. Quando o valor muda, o servidor grava
    `exhaust_power_updated_at` e um registro no historico do ambiente.
    """
    grow = await _get_grow_or_404(grow_id, current_user.id, db)
    previous_power = grow.exhaust_power_pct
    # exclude_unset: null enviado de proposito apaga o campo; campo ausente nao muda.
    changes = body.model_dump(exclude_unset=True)
    new_status = changes.pop("status", None)
    for field, value in changes.items():
        setattr(grow, field, value)
    # Arquivar e restaurar seguem a mesma regra dos endpoints dedicados (plantas juntas).
    if new_status == "abandoned" and grow.status != "abandoned":
        await _archive(db, grow)
    elif new_status == "active" and grow.status == "abandoned":
        await _restore(db, grow, current_user)
    elif new_status is not None:
        grow.status = new_status
    if body.exhaust_power_pct is not None and body.exhaust_power_pct != previous_power:
        now = datetime.now(timezone.utc)
        grow.exhaust_power_updated_at = now
        verb = "registrada como" if previous_power is None else "ajustada para"
        db.add(EnvironmentReading(
            grow_id=grow.id, user_id=current_user.id, observed_at=now,
            notes=f"Potência do exaustor {verb} {body.exhaust_power_pct}%",
            kind="exhaust_power", created_at=now, updated_at=now,
        ))
    await db.commit()
    await db.refresh(grow)
    return _grow_response(grow, await latest_grow_env(db, grow.id))


async def _archive(db: AsyncSession, grow: Grow) -> int:
    """Arquiva o ambiente e as plantas ativas dele, marcando-as. Nao faz commit."""
    result = await db.execute(
        update(Plant)
        .where(Plant.grow_id == grow.id, Plant.user_id == grow.user_id, Plant.is_active == True)  # noqa: E712
        .values(is_active=False, archived_with_grow_id=grow.id)
        .execution_options(synchronize_session=False)
    )
    grow.status = "abandoned"
    grow.archived_at = datetime.now(timezone.utc)
    return result.rowcount or 0


async def _restore(db: AsyncSession, grow: Grow, user: User) -> int:
    """Reativa o ambiente e so as plantas arquivadas junto com ele. Nao faz commit."""
    active_grows = (await db.execute(
        select(func.count(Grow.id)).where(Grow.user_id == user.id, Grow.status == "active")
    )).scalar() or 0
    check_grow_limit(user, active_grows)
    plant_ids = (await db.execute(
        select(Plant.id).where(Plant.archived_with_grow_id == grow.id, Plant.user_id == user.id)
    )).scalars().all()
    if plant_ids:
        active_plants = (await db.execute(
            select(func.count(Plant.id)).where(Plant.user_id == user.id, Plant.is_active == True)  # noqa: E712
        )).scalar() or 0
        # Falha se as plantas restauradas passarem do limite do plano.
        check_plant_limit(user, active_plants + len(plant_ids) - 1)
        await db.execute(
            update(Plant)
            .where(Plant.id.in_(plant_ids))
            .values(is_active=True, archived_with_grow_id=None)
            .execution_options(synchronize_session=False)
        )
    grow.status = "active"
    grow.archived_at = None
    return len(plant_ids)


@router.delete("/{grow_id}", response_model=GrowArchiveResult)
async def delete_grow(
    grow_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Arquiva o ambiente (nao apaga). Na mesma transacao, arquiva as plantas ativas dele e
    marca cada uma com `archived_with_grow_id`. Arquivar de novo nao muda nada."""
    grow = await _get_grow_or_404(grow_id, current_user.id, db)
    archived = 0
    if grow.status != "abandoned":
        archived = await _archive(db, grow)
        await db.commit()
    return GrowArchiveResult(
        message="Grow arquivado com sucesso", grow_id=grow.id, status=grow.status,
        archived_at=grow.archived_at, plants_affected=archived,
    )


@router.post("/{grow_id}/restore", response_model=GrowArchiveResult)
async def restore_grow(
    grow_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Restaura um ambiente arquivado e, na mesma transacao, so as plantas arquivadas junto
    com ele. Plantas arquivadas antes, separadamente, continuam arquivadas. Respeita os limites
    do plano (403). Restaurar um ambiente ativo nao muda nada."""
    grow = await _get_grow_or_404(grow_id, current_user.id, db)
    restored = 0
    if grow.status == "abandoned":
        restored = await _restore(db, grow, current_user)
        await db.commit()
    return GrowArchiveResult(
        message="Grow restaurado com sucesso", grow_id=grow.id, status=grow.status,
        archived_at=grow.archived_at, plants_affected=restored,
    )
