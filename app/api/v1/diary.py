"""
GET /api/v1/diary  — feed cronológico de eventos de todas as plantas do usuário.

Diferença em relação a GET /plants/{id}/events:
- Agrega eventos de TODAS as plantas do usuário em um único feed.
- Cada entrada inclui contexto da planta: plant_name, strain_name, strain_image_url.
- Suporta paginação (limit/offset) e filtros opcionais por planta e tipo de evento.
- As photo_keys de cada evento são convertidas em URLs pré-assinadas para exibição direta.
"""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.event import GrowEvent
from app.models.plant import Plant
from app.models.strain import Strain
from app.models.user import User
from app.services.auth import get_current_user
from app.services.storage import BUCKET_EVENTS, presign_download, strain_image_url_for_response

router = APIRouter()


# ─── Schema ───────────────────────────────────────────────────────────────────


class DiaryEntryResponse(BaseModel):
    # Campos do evento
    id: uuid.UUID
    plant_id: uuid.UUID
    event_type: str
    event_date: datetime
    ppm: float | None
    ph_in: float | None
    ph_out: float | None
    water_volume_ml: float | None
    temperature_c: float | None
    humidity_rh: float | None
    weight_g: float | None
    severity: str | None
    is_flush: bool | None
    notes: str | None
    photo_keys: list[str] | None
    # URLs pré-assinadas prontas para exibição (sem necessidade de chamada extra)
    photo_urls: list[str]
    created_at: datetime

    # Contexto da planta — enriquecimento para o feed do diário
    plant_name: str | None        # strain_name da planta (ex: "Gelato")
    plant_nickname: str | None    # apelido customizado se houver
    strain_name: str | None       # nome da strain vinculada
    strain_image_url: str | None  # thumbnail para o card do diário
    current_phase: str | None     # fase atual da planta

    model_config = {"from_attributes": True}


# ─── Helper ───────────────────────────────────────────────────────────────────


def _event_photo_urls(photo_keys: list[str] | None) -> list[str]:
    """Converte object keys de fotos do evento em URLs pré-assinadas."""
    if not photo_keys:
        return []
    urls: list[str] = []
    for key in photo_keys:
        try:
            urls.append(presign_download(BUCKET_EVENTS, key))
        except Exception:
            pass
    return urls


# ─── Request body para criação em bulk ───────────────────────────────────────


class DiaryBulkCreate(BaseModel):
    """Cria o mesmo evento em múltiplas plantas de uma só vez (usado pelo diário global)."""

    plant_ids: list[uuid.UUID] = Field(..., min_length=1)
    event_type: str
    event_date: datetime
    ppm: float | None = None
    ph_in: float | None = None
    ph_out: float | None = None
    water_volume_ml: float | None = None
    temperature_c: float | None = None
    humidity_rh: float | None = None
    weight_g: float | None = None
    severity: str | None = None
    is_flush: bool | None = None
    notes: str | None = None
    photo_keys: list[str] | None = None


# ─── Routes ───────────────────────────────────────────────────────────────────


@router.post("", response_model=list[DiaryEntryResponse], status_code=status.HTTP_201_CREATED)
async def create_diary_entries(
    body: DiaryBulkCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Cria o mesmo evento em uma ou mais plantas simultaneamente.

    O diário global envia `plant_ids` com todas as plantas selecionadas;
    cada uma recebe um GrowEvent independente, que também aparece na tela
    individual de cada planta (pois ambas usam a mesma tabela grow_events).
    """
    if not body.plant_ids:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="plant_ids nao pode ser vazio")

    # Verifica que todas as plantas pertencem ao usuário
    plants_result = await db.execute(
        select(Plant).where(
            Plant.id.in_(body.plant_ids),
            Plant.user_id == current_user.id,
        )
    )
    owned_plants = {p.id: p for p in plants_result.scalars().all()}

    if len(owned_plants) != len(body.plant_ids):
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Uma ou mais plantas nao pertencem ao usuario")

    # Cria um GrowEvent para cada planta
    event_fields = body.model_dump(exclude={"plant_ids"})
    created_events: list[GrowEvent] = []
    for plant_id in body.plant_ids:
        ev = GrowEvent(plant_id=plant_id, **event_fields)
        db.add(ev)
        created_events.append(ev)

    await db.commit()
    for ev in created_events:
        await db.refresh(ev)

    # Busca strains para enriquecer a resposta
    strain_ids = list({owned_plants[ev.plant_id].strain_id for ev in created_events if owned_plants[ev.plant_id].strain_id})
    strain_map: dict[uuid.UUID, Strain] = {}
    if strain_ids:
        strains_result = await db.execute(select(Strain).where(Strain.id.in_(strain_ids)))
        for s in strains_result.scalars().all():
            strain_map[s.id] = s

    entries: list[DiaryEntryResponse] = []
    for ev in created_events:
        plant = owned_plants.get(ev.plant_id)
        strain = strain_map.get(plant.strain_id) if (plant and plant.strain_id) else None
        entries.append(
            DiaryEntryResponse(
                id=ev.id,
                plant_id=ev.plant_id,
                event_type=ev.event_type,
                event_date=ev.event_date,
                ppm=ev.ppm,
                ph_in=ev.ph_in,
                ph_out=ev.ph_out,
                water_volume_ml=ev.water_volume_ml,
                temperature_c=ev.temperature_c,
                humidity_rh=ev.humidity_rh,
                weight_g=ev.weight_g,
                severity=ev.severity,
                is_flush=ev.is_flush,
                notes=ev.notes,
                photo_keys=ev.photo_keys,
                photo_urls=_event_photo_urls(ev.photo_keys),
                created_at=ev.created_at,
                plant_name=plant.strain_name if plant else None,
                plant_nickname=getattr(plant, "nickname", None),
                strain_name=strain.name if strain else (plant.strain_name if plant else None),
                strain_image_url=strain_image_url_for_response(strain.image_url if strain else None),
                current_phase=plant.current_phase if plant else None,
            )
        )

    return entries


@router.get("", response_model=list[DiaryEntryResponse])
async def get_diary_feed(
    plant_id: uuid.UUID | None = Query(None, description="Filtrar por planta específica"),
    event_type: str | None = Query(None, description="Filtrar por tipo de evento"),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Feed cronológico do diário — todos os eventos do usuário, mais recentes primeiro.

    Cada entrada inclui o contexto da planta (nome da strain, foto, fase) para
    que o Codex possa renderizar cards ricos sem chamadas adicionais à API.
    """
    # 1. Busca todos os plant_ids do usuário
    plants_result = await db.execute(
        select(Plant).where(Plant.user_id == current_user.id, Plant.is_active == True)  # noqa: E712
    )
    plants = plants_result.scalars().all()

    if not plants:
        return []

    # Build plant lookup map: plant_id → Plant
    plant_map: dict[uuid.UUID, Plant] = {p.id: p for p in plants}

    # Optionally filter to a single plant
    target_ids = [plant_id] if plant_id else list(plant_map.keys())

    # 2. Busca os eventos paginados
    q = (
        select(GrowEvent)
        .where(GrowEvent.plant_id.in_(target_ids))
        .order_by(GrowEvent.event_date.desc())
        .limit(limit)
        .offset(offset)
    )
    if event_type:
        q = q.where(GrowEvent.event_type == event_type)

    events_result = await db.execute(q)
    events = events_result.scalars().all()

    if not events:
        return []

    # 3. Busca strains vinculadas (uma query para todas as plantas presentes)
    strain_ids = list({p.strain_id for p in plants if p.strain_id})
    strain_map: dict[uuid.UUID, Strain] = {}
    if strain_ids:
        strains_result = await db.execute(
            select(Strain).where(Strain.id.in_(strain_ids))
        )
        for s in strains_result.scalars().all():
            strain_map[s.id] = s

    # 4. Monta as DiaryEntryResponse
    entries: list[DiaryEntryResponse] = []
    for ev in events:
        plant = plant_map.get(ev.plant_id)
        strain = strain_map.get(plant.strain_id) if (plant and plant.strain_id) else None

        entries.append(
            DiaryEntryResponse(
                id=ev.id,
                plant_id=ev.plant_id,
                event_type=ev.event_type,
                event_date=ev.event_date,
                ppm=ev.ppm,
                ph_in=ev.ph_in,
                ph_out=ev.ph_out,
                water_volume_ml=ev.water_volume_ml,
                temperature_c=ev.temperature_c,
                humidity_rh=ev.humidity_rh,
                weight_g=ev.weight_g,
                severity=ev.severity,
                is_flush=ev.is_flush,
                notes=ev.notes,
                photo_keys=ev.photo_keys,
                photo_urls=_event_photo_urls(ev.photo_keys),
                created_at=ev.created_at,
                # Plant context
                plant_name=plant.strain_name if plant else None,
                plant_nickname=getattr(plant, "nickname", None),
                strain_name=strain.name if strain else (plant.strain_name if plant else None),
                strain_image_url=strain_image_url_for_response(strain.image_url if strain else None),
                current_phase=plant.current_phase if plant else None,
            )
        )

    return entries
