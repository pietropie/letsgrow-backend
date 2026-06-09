import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.models.event import GrowEvent
from app.models.plant import Plant
from app.models.strain import Strain
from app.models.user import User
from app.schemas.event import EventAnalysisResponse, EventCreate, EventResponse, EventUpdate
from app.schemas.plant import (
    BobTipResponse,
    PlantCreate,
    PlantDetailResponse,
    PlantResponse,
    PlantSummary,
    PlantUpdate,
)
from app.schemas.common import MessageResponse
from app.services.auth import get_current_user
from app.services.subscription import check_plant_limit

router = APIRouter()

# Event types considered "watering" for last_watering_* — frontend uses "rega"
WATERING_EVENT_TYPES = ("rega", "watering")


async def _get_plant_or_404(plant_id: uuid.UUID, user_id: uuid.UUID, db: AsyncSession) -> Plant:
    result = await db.execute(
        select(Plant).where(Plant.id == plant_id, Plant.user_id == user_id)
    )
    plant = result.scalar_one_or_none()
    if not plant:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Planta não encontrada")
    return plant


# ---------------------------------------------------------------------------
# Summary helpers — shared between /summaries (batch) and /{plant_id}/summary
# ---------------------------------------------------------------------------

async def _latest_env_reading(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    plant_ids: list[uuid.UUID],
    grow_label: str | None,
    field: str,
):
    """Retorna (valor, event_date) do evento mais recente com `field` preenchido.

    Implementa o "compartilhamento por grow_label" do item 3: se `grow_label`
    não for nulo, considera eventos de TODAS as plantas do usuário com o mesmo
    grow_label; caso contrário, considera apenas os eventos de `plant_ids`
    (tipicamente uma única planta).
    """
    column = getattr(GrowEvent, field)

    if grow_label:
        stmt = (
            select(column, GrowEvent.event_date)
            .join(Plant, Plant.id == GrowEvent.plant_id)
            .where(
                Plant.user_id == user_id,
                Plant.grow_label == grow_label,
                column.is_not(None),
            )
            .order_by(GrowEvent.event_date.desc())
            .limit(1)
        )
    else:
        stmt = (
            select(column, GrowEvent.event_date)
            .where(
                GrowEvent.plant_id.in_(plant_ids),
                column.is_not(None),
            )
            .order_by(GrowEvent.event_date.desc())
            .limit(1)
        )

    result = await db.execute(stmt)
    row = result.first()
    if row is None:
        return None, None
    return row[0], row[1]


async def _latest_watering_event(db: AsyncSession, plant_id: uuid.UUID) -> GrowEvent | None:
    stmt = (
        select(GrowEvent)
        .where(
            GrowEvent.plant_id == plant_id,
            GrowEvent.event_type.in_(WATERING_EVENT_TYPES),
        )
        .order_by(GrowEvent.event_date.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _latest_ppm_event(db: AsyncSession, plant_id: uuid.UUID) -> GrowEvent | None:
    stmt = (
        select(GrowEvent)
        .where(
            GrowEvent.plant_id == plant_id,
            GrowEvent.ppm.is_not(None),
        )
        .order_by(GrowEvent.event_date.desc())
        .limit(1)
    )
    result = await db.execute(stmt)
    return result.scalar_one_or_none()


async def _compute_plant_summary(db: AsyncSession, plant: Plant) -> PlantSummary:
    user_id = plant.user_id

    if plant.grow_label:
        # Plantas do mesmo usuário/grow_label compartilham temperatura e umidade
        result = await db.execute(
            select(Plant.id).where(
                Plant.user_id == user_id, Plant.grow_label == plant.grow_label
            )
        )
        sibling_ids = [row[0] for row in result.all()]
    else:
        sibling_ids = [plant.id]

    last_temp, last_temp_at = await _latest_env_reading(
        db, user_id=user_id, plant_ids=sibling_ids, grow_label=plant.grow_label, field="temperature_c"
    )
    last_hum, last_hum_at = await _latest_env_reading(
        db, user_id=user_id, plant_ids=sibling_ids, grow_label=plant.grow_label, field="humidity_rh"
    )

    last_ppm_event = await _latest_ppm_event(db, plant.id)
    last_ppm = last_ppm_event.ppm if last_ppm_event else None

    last_watering = await _latest_watering_event(db, plant.id)
    last_watering_at = last_watering.event_date if last_watering else None
    last_watering_has_fert = bool(
        last_watering
        and last_watering.ppm is not None
        and last_watering.is_flush is not True
    )

    # Busca imagem da strain — falha silenciosa (sem imagem é ok)
    strain_image_url: str | None = None
    if plant.strain_name:
        strain_result = await db.execute(
            select(Strain.image_url).where(
                func.lower(Strain.name) == func.lower(plant.strain_name)
            ).limit(1)
        )
        row = strain_result.one_or_none()
        if row:
            strain_image_url = row[0]

    return PlantSummary(
        plant_id=plant.id,
        last_temperature_c=last_temp,
        last_temperature_at=last_temp_at,
        last_humidity_rh=last_hum,
        last_humidity_at=last_hum_at,
        last_ppm=last_ppm,
        last_watering_at=last_watering_at,
        last_watering_has_fert=last_watering_has_fert,
        strain_image_url=strain_image_url,
    )


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
    # Verifica limite de plantas do plano antes de criar
    count_result = await db.execute(
        select(func.count(Plant.id)).where(
            Plant.user_id == current_user.id,
            Plant.is_active == True,
        )
    )
    check_plant_limit(current_user, count_result.scalar_one())

    plant = Plant(user_id=current_user.id, **body.model_dump())
    db.add(plant)
    await db.commit()
    await db.refresh(plant)
    return plant


# ---------------------------------------------------------------------------
# GET /plants/grow-labels
# ---------------------------------------------------------------------------

@router.get("/grow-labels", response_model=list[str])
async def list_grow_labels(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Lista os `grow_label` distintos e não-nulos das plantas do usuário,
    ordenados alfabeticamente — usado para popular o dropdown ao criar planta."""
    result = await db.execute(
        select(Plant.grow_label)
        .where(Plant.user_id == current_user.id, Plant.grow_label.is_not(None))
        .distinct()
        .order_by(Plant.grow_label.asc())
    )
    return [row[0] for row in result.all() if row[0]]


# ---------------------------------------------------------------------------
# GET /plants/summaries — batch summary (home cards)
# ---------------------------------------------------------------------------

@router.get("/summaries", response_model=dict[uuid.UUID, PlantSummary])
async def get_plant_summaries(
    is_active: Optional[bool] = Query(default=None, description="Filtrar por status ativo"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Retorna um dict `plant_id -> PlantSummary` para todas as plantas do usuário.

    Pensado para o card da home: o front faz uma única chamada e recebe o resumo
    (última temperatura/umidade/PPM/rega) de todas as plantas de uma vez.
    """
    stmt = select(Plant).where(Plant.user_id == current_user.id)
    if is_active is not None:
        stmt = stmt.where(Plant.is_active == is_active)
    result = await db.execute(stmt)
    plants = result.scalars().all()

    summaries: dict[uuid.UUID, PlantSummary] = {}
    for plant in plants:
        summaries[plant.id] = await _compute_plant_summary(db, plant)
    return summaries


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
# GET /plants/{plant_id}/summary
# ---------------------------------------------------------------------------

@router.get("/{plant_id}/summary", response_model=PlantSummary)
async def get_plant_summary(
    plant_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Resumo para o card da home: última temperatura/umidade (compartilhadas
    entre plantas do mesmo grow_label), último PPM e última rega desta planta."""
    plant = await _get_plant_or_404(plant_id, current_user.id, db)
    return await _compute_plant_summary(db, plant)


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

    # Invalida cache de dica do Bob para esta planta (novo evento pode mudar o cenário)
    from app.services.rag import invalidate_plant_tip_cache
    invalidate_plant_tip_cache(plant_id)

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


# ---------------------------------------------------------------------------
# POST /plants/{plant_id}/events/{event_id}/analyze
# ---------------------------------------------------------------------------

@router.post("/{plant_id}/events/{event_id}/analyze", response_model=EventAnalysisResponse)
async def analyze_event(
    plant_id: uuid.UUID,
    event_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Analisa as fotos de um evento do diário com IA multimodal (requer um
    provedor com suporte a visão configurado em /admin/ai-panel — ex.: Gemini)."""
    plant = await _get_plant_or_404(plant_id, current_user.id, db)
    result = await db.execute(
        select(GrowEvent).where(GrowEvent.id == event_id, GrowEvent.plant_id == plant_id)
    )
    event = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evento não encontrado")

    if not event.photo_keys:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Este evento não possui fotos para analisar",
        )

    from app.services.rag import analyze_event_photos

    try:
        analysis = await analyze_event_photos(db, plant=plant, event=event)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Falha ao analisar fotos com IA: {exc}",
        )

    return EventAnalysisResponse(
        event_id=event.id,
        status=analysis["status"],
        resumo=analysis["resumo"],
        problemas=analysis["problemas"],
        recomendacoes=analysis["recomendacoes"],
        observacao_foto=analysis.get("observacao_foto"),
        photos_analyzed=len(event.photo_keys),
    )


@router.get("/{plant_id}/bob-tip", response_model=BobTipResponse | None)
async def get_bob_tip(
    plant_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Retorna uma dica contextual do Bob para a planta, baseada em regras +
    LLM. O resultado é cacheado por ~4h para evitar chamadas repetidas ao LLM.
    Retorna 204/null se não houver cenário relevante no momento."""
    plant = await _get_plant_or_404(plant_id, current_user.id, db)

    # Carrega os 30 eventos mais recentes (suficiente para as regras de rega/fert)
    result = await db.execute(
        select(GrowEvent)
        .where(GrowEvent.plant_id == plant_id)
        .order_by(GrowEvent.event_date.desc())
        .limit(30)
    )
    events = result.scalars().all()

    from app.services.rag import generate_plant_tip
    tip = await generate_plant_tip(db, plant=plant, events=events)
    return tip  # None serializa como 204/null no FastAPI com response_model=X|None
