"""
Serviço de briefing diário do Bob.

Responsabilidades:
  1. Computar quais plantas têm ações urgentes (rega, topping, desfolha, flush…).
  2. Gerar um texto amigável e proativo via LLM — máximo 3 frases, tom de consultor.
  3. Retornar título + corpo prontos para exibição no app e/ou push notification.

Usado por:
  - GET /plants/daily-brief  → exibe card proativo na home (cached 24h no Redis)
  - POST /admin/push/daily   → dispara push para todos os usuários ativos
"""
import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import GrowEvent
from app.models.plant import Plant
from app.rag.prompts import (
    _TRAINING_TYPES,
    _WATERING_TYPES,
    _compute_next_steps,
)
from app.services import ai_provider
from app.services.redis_client import _get_redis

logger = logging.getLogger(__name__)

_BRIEF_TTL = 86_400  # 24 horas


@dataclass
class DailyBrief:
    title: str          # ex: "🌿 Bob: sua Gelato precisa de atenção"
    body: str           # texto gerado pelo LLM (2-3 frases)
    urgent_count: int   # quantas ações urgentes existem (para priorizar push)


# ─── Cache Redis ──────────────────────────────────────────────────────────────

def _cache_key(user_id: uuid.UUID) -> str:
    return f"daily_brief:{user_id}"


async def get_cached_brief(user_id: uuid.UUID) -> DailyBrief | None:
    try:
        r = _get_redis()
        title = await r.get(f"{_cache_key(user_id)}:title")
        body = await r.get(f"{_cache_key(user_id)}:body")
        urgent = await r.get(f"{_cache_key(user_id)}:urgent")
        if title and body:
            return DailyBrief(title=title, body=body, urgent_count=int(urgent or 0))
    except Exception as exc:
        logger.warning("Redis get daily_brief falhou: %s", exc)
    return None


async def cache_brief(user_id: uuid.UUID, brief: DailyBrief) -> None:
    try:
        r = _get_redis()
        key = _cache_key(user_id)
        await r.set(f"{key}:title", brief.title, ex=_BRIEF_TTL)
        await r.set(f"{key}:body", brief.body, ex=_BRIEF_TTL)
        await r.set(f"{key}:urgent", str(brief.urgent_count), ex=_BRIEF_TTL)
    except Exception as exc:
        logger.warning("Redis set daily_brief falhou: %s", exc)


async def invalidate_brief(user_id: uuid.UUID) -> None:
    try:
        r = _get_redis()
        key = _cache_key(user_id)
        await r.delete(f"{key}:title", f"{key}:body", f"{key}:urgent")
    except Exception:
        pass


# ─── Análise de plantas ───────────────────────────────────────────────────────

async def _load_plant_steps(db: AsyncSession, plant: Plant, today: date) -> list[str]:
    """Carrega últimos 15 eventos da planta e computa ações sugeridas."""
    ev_result = await db.execute(
        select(GrowEvent)
        .where(GrowEvent.plant_id == plant.id)
        .order_by(GrowEvent.event_date.desc())
        .limit(15)
    )
    events = ev_result.scalars().all()

    # Treinamentos
    trainings_done: set[str] = set()
    for ev in events:
        if ev.event_type in _TRAINING_TYPES:
            trainings_done.add(ev.event_type)

    # Análise de rega
    watering_evs = [ev for ev in events if ev.event_type in _WATERING_TYPES]
    days_since: float | None = None
    avg_interval: float | None = None

    if watering_evs:
        last = watering_evs[0].event_date
        if isinstance(last, datetime):
            last = last.date()
        days_since = float((today - last).days)

        if len(watering_evs) >= 2:
            ivals: list[int] = []
            for i in range(len(watering_evs) - 1):
                d0 = watering_evs[i].event_date
                d1 = watering_evs[i + 1].event_date
                if isinstance(d0, datetime):
                    d0 = d0.date()
                if isinstance(d1, datetime):
                    d1 = d1.date()
                diff = (d0 - d1).days
                if diff > 0:
                    ivals.append(diff)
            if ivals:
                avg_interval = sum(ivals) / len(ivals)

    return _compute_next_steps(
        plant=plant,
        trainings_done=trainings_done,
        days_since_watering=days_since,
        avg_watering_interval=avg_interval,
        today=today,
    )


# ─── Geração via LLM ─────────────────────────────────────────────────────────

_BRIEF_SYSTEM = """Voce e Bob, consultor de cultivo do LetsGrow.
Gere um briefing diario CURTO (2-3 frases) para o cultivador sobre o que precisa de atencao hoje.
- Priorize a acao mais urgente
- Explique brevemente POR QUE ela importa (em linguagem simples para iniciantes)
- Termine com encorajamento ou uma dica rapida
- Use portugues brasileiro informal e amigavel
- NAO use listas, apenas texto corrido
- Maximo 60 palavras no total"""


async def _generate_brief_text(plant_steps: list[tuple[str, list[str]]]) -> str:
    """
    plant_steps: [(strain_name, [passo1, passo2, ...]), ...]
    Retorna texto do briefing gerado pelo LLM.
    """
    # Monta o contexto para o LLM
    lines: list[str] = []
    for strain_name, steps in plant_steps:
        if steps:
            lines.append(f"{strain_name}:")
            for step in steps[:2]:  # Máximo 2 por planta para não sobrecarregar o prompt
                lines.append(f"  - {step}")

    if not lines:
        return "Tudo em dia no seu cultivo! Aproveite para observar as plantas e registrar qualquer mudanca no diario."

    context = "\n".join(lines)
    human_msg = f"Situacao atual das plantas:\n{context}\n\nGere o briefing diario."

    try:
        llm = await ai_provider.get_chat_model()
        messages = [
            SystemMessage(content=_BRIEF_SYSTEM),
            HumanMessage(content=human_msg),
        ]
        resp = await llm.ainvoke(messages)
        return resp.content.strip()
    except Exception as exc:
        logger.error("LLM brief falhou: %s", exc)
        # Fallback sem LLM — usa o primeiro passo urgente encontrado
        for _, steps in plant_steps:
            if steps:
                return steps[0]
        return "Confira suas plantas hoje e registre qualquer novidade no diario."


# ─── API principal ────────────────────────────────────────────────────────────

async def generate_daily_brief(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    force_refresh: bool = False,
) -> DailyBrief:
    """
    Gera (ou retorna do cache) o briefing diario para um usuario.

    force_refresh=True ignora o cache e regera sempre (usado pelo job de push).
    """
    if not force_refresh:
        cached = await get_cached_brief(user_id)
        if cached:
            return cached

    today = date.today()

    # Busca plantas ativas
    plants_result = await db.execute(
        select(Plant)
        .where(Plant.user_id == user_id, Plant.is_active == True)  # noqa: E712
        .order_by(Plant.created_at.desc())
        .limit(10)
    )
    plants = plants_result.scalars().all()

    if not plants:
        brief = DailyBrief(
            title="🌱 Bob: bem-vindo ao LetsGrow!",
            body="Cadastre sua primeira planta para que eu possa acompanhar seu cultivo e dar dicas personalizadas todos os dias.",
            urgent_count=0,
        )
        await cache_brief(user_id, brief)
        return brief

    # Computa ações por planta
    plant_steps: list[tuple[str, list[str]]] = []
    total_urgent = 0

    for plant in plants:
        steps = await _load_plant_steps(db, plant, today)
        plant_steps.append((plant.strain_name or "Planta", steps))
        # Conta urgentes (contêm "URGENTE" ou "COLHEITA" ou "FLUSH")
        total_urgent += sum(
            1 for s in steps
            if any(kw in s.upper() for kw in ("URGENTE", "COLHEITA", "FLUSH"))
        )

    # Gera texto via LLM
    body = await _generate_brief_text(plant_steps)

    # Escolhe título com base na urgência
    if total_urgent > 0:
        first_urgent_plant = next(
            (name for name, steps in plant_steps
             if any(kw in " ".join(steps).upper() for kw in ("URGENTE", "COLHEITA", "FLUSH"))),
            plants[0].strain_name or "Planta",
        )
        title = f"🚨 Bob: {first_urgent_plant} precisa de atenção agora"
    elif any(steps for _, steps in plant_steps):
        title = f"🌿 Bob: dicas de hoje para o seu cultivo"
    else:
        title = "✅ Bob: tudo em dia no cultivo!"

    brief = DailyBrief(title=title, body=body, urgent_count=total_urgent)
    await cache_brief(user_id, brief)
    return brief
