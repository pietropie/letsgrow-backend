"""
GET  /api/v1/diary  — feed cronológico de eventos de todas as plantas do usuário.
POST /api/v1/diary  — cria o mesmo evento em múltiplas plantas de uma só vez.

Diferença em relação a GET /plants/{id}/events:
- Agrega eventos de TODAS as plantas do usuário em um único feed.
- Cada entrada inclui contexto da planta: plant_name, strain_name, strain_image_url.
- Suporta paginação (limit/offset) e filtros opcionais por planta e tipo de evento.
- As photo_keys de cada evento são convertidas em URLs pré-assinadas para exibição direta.

Nota: Plant não possui FK strain_id — o vínculo é feito via Plant.strain_name → Strain.name.
"""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from typing import Any

from pydantic import AwareDatetime, BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.event import GrowEvent
from app.models.plant import Plant
from app.models.strain import Strain
from app.models.user import User
from app.services.auth import get_current_user
from app.services.storage import BUCKET_EVENTS, presign_download, strain_image_url_for_response
from app.services.upload_access import owns_upload, require_owned_event_photos

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
    # URLs pré-assinadas prontas para exibição
    photo_urls: list[str]
    created_at: datetime

    # Contexto da planta
    plant_name: str | None
    plant_nickname: str | None
    strain_name: str | None
    strain_image_url: str | None
    current_phase: str | None

    model_config = {"from_attributes": True}


# ─── Request body para criação em bulk ───────────────────────────────────────


class DiaryBulkCreate(BaseModel):
    """Cria o mesmo evento em múltiplas plantas de uma só vez (usado pelo diário global)."""

    plant_ids: list[uuid.UUID] = Field(..., min_length=1)
    grow_id: uuid.UUID | None = None
    event_type: str
    event_date: datetime
    ppm: float | None = None
    ph_in: float | None = None
    water_volume_ml: float | None = None
    ph_out: float | None = None
    ec_out: float | None = None
    has_runoff: bool | None = None
    nutrient_subtype: str | None = None
    is_flush: bool | None = None
    training_subtype: str | None = None
    trichome_clear_pct: int | None = None
    trichome_milky_pct: int | None = None
    trichome_amber_pct: int | None = None
    temperature_c: float | None = None
    humidity_rh: float | None = None
    weight_g: float | None = None
    severity: str | None = None
    notes: str | None = None
    photo_keys: list[str] | None = None
    metadata: dict[str, Any] | None = None


# ─── Helpers ──────────────────────────────────────────────────────────────────


def _event_photo_urls(photo_keys: list[str] | None) -> list[str]:
    if not photo_keys:
        return []
    urls: list[str] = []
    for key in photo_keys:
        try:
            urls.append(presign_download(BUCKET_EVENTS, key))
        except Exception:
            pass
    return urls


async def _build_strain_image_map(db: AsyncSession, strain_names: set[str]) -> dict[str, str | None]:
    """
    Retorna {strain_name_lower: strain_image_url} para os nomes fornecidos.
    Plant não tem FK strain_id, então buscamos por nome (case-insensitive).
    """
    if not strain_names:
        return {}

    result = await db.execute(
        select(Strain.name, Strain.image_url).where(
            func.lower(Strain.name).in_([n.lower() for n in strain_names])
        )
    )
    return {
        row[0].lower(): strain_image_url_for_response(row[1])
        for row in result.fetchall()
    }


def _build_entry(
    ev: GrowEvent,
    plant: Plant | None,
    strain_image_map: dict[str, str | None],
) -> DiaryEntryResponse:
    strain_name = plant.strain_name if plant else None
    strain_image_url = strain_image_map.get(strain_name.lower()) if strain_name else None
    # Recheck persisted keys too: old records may predate upload validation.
    photo_keys = [
        key for key in (ev.photo_keys or [])
        if plant is not None and owns_upload(BUCKET_EVENTS, key, plant.user_id)
    ]

    return DiaryEntryResponse(
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
        photo_keys=photo_keys or None,
        photo_urls=_event_photo_urls(photo_keys),
        created_at=ev.created_at,
        plant_name=(plant.display_name or strain_name or "Planta") if plant else None,
        plant_nickname=plant.display_name if plant else None,
        strain_name=strain_name,
        strain_image_url=strain_image_url,
        current_phase=plant.current_phase if plant else None,
    )


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
    require_owned_event_photos(body.photo_keys, current_user.id)
    plant_ids = list(dict.fromkeys(body.plant_ids))
    # Verifica que todas as plantas pertencem ao usuário
    plants_result = await db.execute(
        select(Plant).where(
            Plant.id.in_(plant_ids),
            Plant.user_id == current_user.id,
        )
    )
    owned_plants: dict[uuid.UUID, Plant] = {p.id: p for p in plants_result.scalars().all()}

    if len(owned_plants) != len(set(body.plant_ids)):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Uma ou mais plantas nao pertencem ao usuario",
        )

    if body.grow_id is not None:
        mismatched = [p for p in owned_plants.values() if p.grow_id != body.grow_id]
        if mismatched:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Todas as plantas do registro multiplo devem pertencer ao mesmo grow",
            )

    event_fields = body.model_dump(exclude={"plant_ids", "grow_id"})
    event_fields["event_metadata"] = event_fields.pop("metadata", None)
    created_events: list[GrowEvent] = []
    for plant_id in plant_ids:
        ev = GrowEvent(plant_id=plant_id, **event_fields)
        db.add(ev)
        created_events.append(ev)

    await db.commit()
    for ev in created_events:
        await db.refresh(ev)

    from app.services.bob_accountability import run_event_side_effects

    for ev in created_events:
        plant = owned_plants.get(ev.plant_id)
        if plant is not None:
            await run_event_side_effects(db, plant, ev, current_user.id)
    try:
        await db.commit()
    except Exception:
        await db.rollback()

    # Busca imagens de strain por nome
    strain_names = {p.strain_name for p in owned_plants.values() if p.strain_name}
    strain_image_map = await _build_strain_image_map(db, strain_names)

    return [_build_entry(ev, owned_plants.get(ev.plant_id), strain_image_map) for ev in created_events]


@router.get("", response_model=list[DiaryEntryResponse])
async def get_diary_feed(
    plant_id: uuid.UUID | None = Query(None, description="Filtrar por planta específica"),
    grow_id: uuid.UUID | None = Query(None, description="Filtrar por grow"),
    event_type: str | None = Query(None, description="Filtrar por tipo de evento"),
    start_at: AwareDatetime | None = Query(None, description="Início inclusivo do período, com timezone"),
    end_at: AwareDatetime | None = Query(None, description="Fim exclusivo do período, com timezone"),
    include_archived: bool = Query(False, description="Incluir plantas arquivadas do usuário"),
    limit: int = Query(30, ge=1, le=100),
    offset: int = Query(0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """
    Feed cronológico do diário — todos os eventos do usuário, mais recentes primeiro.

    Cada entrada inclui o contexto da planta (nome da strain, foto, fase) para
    que o app possa renderizar cards ricos sem chamadas adicionais à API.
    """
    if start_at is not None and end_at is not None and start_at >= end_at:
        raise HTTPException(status_code=422, detail="O fim do período deve ser posterior ao início")
    # 1. Plantas do usuário, incluindo histórico arquivado somente quando solicitado.
    plant_query = select(Plant).where(Plant.user_id == current_user.id)
    if not include_archived:
        plant_query = plant_query.where(Plant.is_active.is_(True))
    plants_result = await db.execute(plant_query)
    plants = plants_result.scalars().all()

    if not plants:
        return []

    plant_map: dict[uuid.UUID, Plant] = {p.id: p for p in plants}
    target_ids = [
        p.id for p in plants
        if (plant_id is None or p.id == plant_id)
        and (grow_id is None or p.grow_id == grow_id)
    ]
    if not target_ids:
        return []

    # 2. Eventos paginados
    q = (
        select(GrowEvent)
        .join(Plant, Plant.id == GrowEvent.plant_id)
        .where(GrowEvent.plant_id.in_(target_ids), Plant.user_id == current_user.id)
        .order_by(GrowEvent.event_date.desc(), GrowEvent.id.desc())
        .limit(limit)
        .offset(offset)
    )
    if event_type:
        q = q.where(GrowEvent.event_type == event_type)
    if start_at is not None:
        q = q.where(GrowEvent.event_date >= start_at.astimezone(timezone.utc))
    if end_at is not None:
        q = q.where(GrowEvent.event_date < end_at.astimezone(timezone.utc))

    events_result = await db.execute(q)
    events = events_result.scalars().all()

    if not events:
        return []

    # 3. Imagens de strain (busca por nome, não por FK)
    strain_names = {p.strain_name for p in plants if p.strain_name}
    strain_image_map = await _build_strain_image_map(db, strain_names)

    # 4. Monta as respostas
    return [_build_entry(ev, plant_map.get(ev.plant_id), strain_image_map) for ev in events]
