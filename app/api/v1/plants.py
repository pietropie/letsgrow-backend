import logging
import uuid
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.database import get_db
from app.services.storage import BUCKET_EVENTS, strain_image_url_for_response
from app.services.upload_access import owns_upload, require_owned_event_photos
from app.services.grow_env import latest_grow_measurements
from app.models.event import GrowEvent
from app.models.grow import Grow
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

logger = logging.getLogger(__name__)

router = APIRouter()

# Event types considered "watering" for last_watering_* — frontend uses "rega"
WATERING_EVENT_TYPES = ("rega", "watering")


def _private_event_response(event: GrowEvent, user_id: uuid.UUID) -> EventResponse:
    response = EventResponse.model_validate(event)
    return response.model_copy(update={
        "photo_keys": [
            key for key in (event.photo_keys or [])
            if owns_upload(BUCKET_EVENTS, key, user_id)
        ] or None,
    })


async def _run_bob_event_hooks(
    db: AsyncSession,
    plant: Plant,
    event: GrowEvent,
    user_id,
) -> None:
    """Anomalia + fecha recomendacoes cobertas pelo evento. Nunca quebra o request."""
    from app.services.bob_accountability import run_event_side_effects

    await run_event_side_effects(db, plant, event, user_id)
    try:
        await db.commit()
    except Exception as exc:
        logger.warning("Bob event hook commit falhou: %s", exc)
        try:
            await db.rollback()
        except Exception:
            pass


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
    measurements = await latest_grow_measurements(db, plant.grow_id)
    temperature = measurements['temperature']
    humidity = measurements['humidity']

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
            strain_image_url = strain_image_url_for_response(row[0])

    return PlantSummary(
        plant_id=plant.id,
        temperature=temperature,
        humidity=humidity,
        last_temperature_c=temperature.value,
        last_temperature_at=temperature.observed_at,
        last_humidity_rh=humidity.value,
        last_humidity_at=humidity.observed_at,
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
    grow_id: Optional[uuid.UUID] = Query(default=None, description="Filtrar por grow"),
    grow_label: Optional[str] = Query(default=None, description="Legado — preferir grow_id"),
    is_active: Optional[bool] = Query(default=None, description="Filtrar por status ativo"),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Lista todas as plantas do usuário autenticado."""
    stmt = select(Plant).where(Plant.user_id == current_user.id)
    if grow_id is not None:
        stmt = stmt.where(Plant.grow_id == grow_id)
    elif grow_label is not None:
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
    """Cria uma nova planta vinculada a um grow do usuario."""
    grow_result = await db.execute(
        select(Grow).where(Grow.id == body.grow_id, Grow.user_id == current_user.id)
    )
    grow = grow_result.scalar_one_or_none()
    if not grow:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grow não encontrado")
    if grow.status == "abandoned":
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Não é possível criar planta em um grow arquivado",
        )

    count_result = await db.execute(
        select(func.count(Plant.id)).where(
            Plant.user_id == current_user.id,
            Plant.is_active == True,  # noqa: E712
        )
    )
    check_plant_limit(current_user, count_result.scalar_one())

    payload = body.model_dump()
    payload["grow_label"] = grow.name
    plant = Plant(user_id=current_user.id, **payload)
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
    response = PlantDetailResponse.model_validate(plant)
    return response.model_copy(update={
        "events": [_private_event_response(event, current_user.id) for event in plant.events],
    })


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
    updates = body.model_dump(exclude_unset=True)
    if not (updates.get("display_name", plant.display_name) or updates.get("strain_name", plant.strain_name)):
        raise HTTPException(status_code=422, detail="Informe um nome para identificar a planta")
    if "grow_id" in updates and updates["grow_id"] is not None:
        grow_result = await db.execute(
            select(Grow).where(Grow.id == updates["grow_id"], Grow.user_id == current_user.id)
        )
        grow = grow_result.scalar_one_or_none()
        if not grow:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Grow não encontrado")
        if grow.status == "abandoned":
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Não é possível mover planta para um grow arquivado",
            )
        updates["grow_label"] = grow.name
    for field, value in updates.items():
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
    return [_private_event_response(event, current_user.id) for event in result.scalars().all()]


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
    require_owned_event_photos(body.photo_keys, current_user.id)

    # Remapear "metadata" (Pydantic) → "event_metadata" (SQLAlchemy)
    event_data = body.model_dump()
    event_data["event_metadata"] = event_data.pop("metadata", None)
    event = GrowEvent(plant_id=plant_id, **event_data)
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

    await _run_bob_event_hooks(db, plant, event, current_user.id)

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
    plant = await _get_plant_or_404(plant_id, current_user.id, db)
    result = await db.execute(
        select(GrowEvent).where(GrowEvent.id == event_id, GrowEvent.plant_id == plant_id)
    )
    event = result.scalar_one_or_none()
    if not event:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Evento não encontrado")

    # Remapear "metadata" (Pydantic) → "event_metadata" (SQLAlchemy)
    update_data = body.model_dump(exclude_unset=True)
    require_owned_event_photos(update_data.get("photo_keys", event.photo_keys), current_user.id)
    if "metadata" in update_data:
        update_data["event_metadata"] = update_data.pop("metadata")
    for field, value in update_data.items():
        setattr(event, field, value)
    await db.commit()
    await db.refresh(event)

    await _run_bob_event_hooks(db, plant, event, current_user.id)

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
    quote_id: Optional[uuid.UUID] = Query(default=None, description="Cotação confirmada, se a análise tiver custo"),
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

    require_owned_event_photos(event.photo_keys, current_user.id)
    from app.api.v1.credits import start_paid_task
    from app.models.credits import CreditQuote
    from app.services.credits import capture, release
    from app.services.rag import analyze_event_photos

    # Limite mensal de consultas e, se a analise tiver custo, reserva dos creditos.
    reservation = await start_paid_task(db, current_user, "photo_analysis", quote_id)
    reservation_id = reservation.id if reservation is not None else None

    async def _release_reservation() -> None:
        if reservation_id is None:
            return
        await db.rollback()
        quote = await db.get(CreditQuote, reservation_id)
        if quote is not None:
            await release(db, quote, "Falha técnica")

    try:
        analysis = await analyze_event_photos(db, plant=plant, event=event)
    except RuntimeError as exc:
        await _release_reservation()
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=str(exc))
    except Exception as exc:
        await _release_reservation()
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Falha ao analisar fotos com IA: {exc}",
        )

    # Incrementa contador de uso de IA (planos com limite: free, jardineiro)
    current_user.ai_queries_this_month += 1
    if reservation is not None:
        await capture(db, reservation, task_ref=str(event.id))
    await db.commit()

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


# ─── Daily Brief ──────────────────────────────────────────────────────────────

class DailyBriefResponse(BaseModel):
    """Briefing diário do Bob — gerado via LLM e cacheado 24h."""
    title: str
    body: str
    urgent_count: int = 0
    severity: str = "ok"                    # "ok" | "attention" | "urgent"
    plant_id: str | None = None             # UUID da planta mais relevante
    plant_name: str | None = None           # nome da strain mais relevante
    cta_prompt: str = ""                    # pergunta pré-preenchida para abrir o Bob
    generated_at: str = ""                  # ISO UTC timestamp da geração
    reason_tags: list[str] = []             # ["rega", "floração", "flush"] (máx 4)


@router.get("/daily-brief", response_model=DailyBriefResponse)
async def get_daily_brief(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Retorna o briefing diário do Bob para o usuário: resumo proativo do que
    precisa de atenção no cultivo hoje, gerado via LLM e cacheado 24h no Redis.

    O frontend chama este endpoint ao abrir o app (1x por sessão) e exibe
    um card "Bob hoje" no topo da home screen.
    """
    from app.services.daily_brief import generate_daily_brief
    brief = await generate_daily_brief(db, current_user.id)
    return DailyBriefResponse(
        title=brief.title,
        body=brief.body,
        urgent_count=brief.urgent_count,
        severity=brief.severity,
        plant_id=str(brief.plant_id) if brief.plant_id else None,
        plant_name=brief.plant_name,
        cta_prompt=brief.cta_prompt,
        generated_at=brief.generated_at,
        reason_tags=brief.reason_tags,
    )
