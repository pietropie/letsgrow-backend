"""
Serviço de briefing diário do Bob.

Responsabilidades:
  1. Computar quais plantas têm ações urgentes (rega, topping, desfolha, flush…).
  2. Gerar um texto amigável e proativo via LLM — máximo 3 frases, tom de consultor.
  3. Retornar título + corpo + metadados prontos para o card hero da home e push.

Usado por:
  - GET /plants/daily-brief  → exibe card proativo na home (cached 24h no Redis)
  - POST /admin/push/daily   → dispara push para todos os usuários ativos
"""
import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from typing import Literal

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

Severity = Literal["ok", "attention", "urgent"]


@dataclass
class DailyBrief:
    title: str                          # ex: "🚨 Bob: Gelato precisa de atenção agora"
    body: str                           # texto gerado pelo LLM (2-3 frases)
    urgent_count: int                   # quantas ações urgentes (para priorizar push)
    severity: Severity = "ok"           # ok | attention | urgent — controla cor do card
    plant_id: str | None = None         # UUID da planta mais relevante (str para JSON)
    plant_name: str | None = None       # nome da strain mais relevante
    cta_prompt: str = ""                # pergunta pré-preenchida para abrir o Bob
    generated_at: str = ""             # ISO UTC timestamp da geração
    reason_tags: list[str] = field(default_factory=list)  # ["rega","floração","flush"]


# ─── Cache Redis (JSON único por usuário) ─────────────────────────────────────

def _cache_key(user_id: uuid.UUID) -> str:
    return f"daily_brief_v2:{user_id}"


async def get_cached_brief(user_id: uuid.UUID) -> DailyBrief | None:
    try:
        r = _get_redis()
        raw = await r.get(_cache_key(user_id))
        if raw:
            data = json.loads(raw)
            return DailyBrief(**data)
    except Exception as exc:
        logger.warning("Redis get daily_brief falhou: %s", exc)
    return None


async def cache_brief(user_id: uuid.UUID, brief: DailyBrief) -> None:
    try:
        r = _get_redis()
        payload = json.dumps({
            "title": brief.title,
            "body": brief.body,
            "urgent_count": brief.urgent_count,
            "severity": brief.severity,
            "plant_id": brief.plant_id,
            "plant_name": brief.plant_name,
            "cta_prompt": brief.cta_prompt,
            "generated_at": brief.generated_at,
            "reason_tags": brief.reason_tags,
        })
        await r.set(_cache_key(user_id), payload, ex=_BRIEF_TTL)
    except Exception as exc:
        logger.warning("Redis set daily_brief falhou: %s", exc)


async def invalidate_brief(user_id: uuid.UUID) -> None:
    try:
        r = _get_redis()
        # Remove tanto o cache novo quanto as chaves legadas (v1)
        key = _cache_key(user_id)
        old = f"daily_brief:{user_id}"
        await r.delete(key, f"{old}:title", f"{old}:body", f"{old}:urgent")
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

    trainings_done: set[str] = set()
    for ev in events:
        if ev.event_type in _TRAINING_TYPES:
            trainings_done.add(ev.event_type)

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


# ─── Extração de reason_tags ──────────────────────────────────────────────────

_TAG_KEYWORDS: list[tuple[str, str]] = [
    ("REGA", "rega"),
    ("FLUSH", "flush"),
    ("COLHEITA", "colheita"),
    ("TOPPING", "poda"),
    ("PODA", "poda"),
    ("DESFOLHA", "desfolha"),
    ("NUTRIÇÃO", "nutrição"),
    ("NUTRIENTES", "nutrição"),
    ("LST", "treinamento"),
    ("SUPERCROP", "treinamento"),
    ("SCROG", "treinamento"),
    ("TRANSPLANTE", "transplante"),
]

_PHASE_TAGS: dict[str, str] = {
    "germination": "germinação",
    "seedling": "mudas",
    "veg": "vegetativo",
    "flower": "floração",
    "harvest": "colheita",
}


def _extract_reason_tags(
    plant_steps: list[tuple[str, list[str]]],
    plants: list[Plant],
) -> list[str]:
    """Deriva tags legíveis a partir dos steps + fases das plantas."""
    tags: list[str] = []
    seen: set[str] = set()

    full_text = " ".join(s for _, steps in plant_steps for s in steps).upper()

    for keyword, tag in _TAG_KEYWORDS:
        if keyword in full_text and tag not in seen:
            tags.append(tag)
            seen.add(tag)

    for plant in plants:
        phase_tag = _PHASE_TAGS.get(plant.current_phase or "", "")
        if phase_tag and phase_tag not in seen:
            tags.append(phase_tag)
            seen.add(phase_tag)

    return tags[:4]  # máximo 4 chips no card


# ─── CTA prompt pré-preenchido ────────────────────────────────────────────────

def _build_cta_prompt(
    severity: Severity,
    plant_name: str | None,
    reason_tags: list[str],
) -> str:
    name = plant_name or "meu cultivo"
    if severity == "urgent":
        topic = f" sobre {reason_tags[0]}" if reason_tags else ""
        return f"Bob, o que precisa de atenção urgente na {name} hoje{topic}? Me explica e traz ações práticas."
    if severity == "attention":
        topic = f" (especialmente sobre {', '.join(reason_tags[:2])})" if reason_tags else ""
        return f"Bob, me conta o que você recomenda para a {name} hoje{topic}."
    return f"Bob, tudo certo com o cultivo hoje? Tem alguma coisa para observar na {name}?"


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
    lines: list[str] = []
    for strain_name, steps in plant_steps:
        if steps:
            lines.append(f"{strain_name}:")
            for step in steps[:2]:
                lines.append(f"  - {step}")

    if not lines:
        return "Tudo em dia no seu cultivo! Aproveite para observar as plantas e registrar qualquer mudança no diário."

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
        for _, steps in plant_steps:
            if steps:
                return steps[0]
        return "Confira suas plantas hoje e registre qualquer novidade no diário."


# ─── API principal ────────────────────────────────────────────────────────────

async def generate_daily_brief(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    force_refresh: bool = False,
) -> DailyBrief:
    """
    Gera (ou retorna do cache) o briefing diário para um usuário.
    force_refresh=True ignora o cache (usado pelo job de push).
    """
    if not force_refresh:
        cached = await get_cached_brief(user_id)
        if cached:
            return cached

    today = date.today()
    now_iso = datetime.now(timezone.utc).isoformat()

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
            severity="ok",
            cta_prompt="Bob, como começo a usar o LetsGrow para acompanhar meu cultivo?",
            generated_at=now_iso,
        )
        await cache_brief(user_id, brief)
        return brief

    # Computa ações por planta
    plant_steps: list[tuple[str, list[str]]] = []
    total_urgent = 0
    urgent_plant: Plant | None = None

    for plant in plants:
        steps = await _load_plant_steps(db, plant, today)
        plant_steps.append((plant.strain_name or "Planta", steps))
        n_urgent = sum(
            1 for s in steps
            if any(kw in s.upper() for kw in ("URGENTE", "COLHEITA", "FLUSH"))
        )
        if n_urgent and urgent_plant is None:
            urgent_plant = plant
        total_urgent += n_urgent

    # Severity
    has_any_steps = any(steps for _, steps in plant_steps)
    if total_urgent > 0:
        severity: Severity = "urgent"
    elif has_any_steps:
        severity = "attention"
    else:
        severity = "ok"

    # Planta principal (urgente ou a primeira)
    main_plant = urgent_plant or plants[0]
    plant_id_str = str(main_plant.id)
    plant_name = main_plant.strain_name or "Planta"

    # Tags
    reason_tags = _extract_reason_tags(plant_steps, plants)

    # Gera texto via LLM
    body = await _generate_brief_text(plant_steps)

    # Título
    if severity == "urgent":
        title = f"🚨 Bob: {plant_name} precisa de atenção agora"
    elif severity == "attention":
        title = f"🌿 Bob: dicas de hoje para o seu cultivo"
    else:
        title = "✅ Bob: tudo em dia no cultivo!"

    # CTA prompt
    cta_prompt = _build_cta_prompt(severity, plant_name, reason_tags)

    brief = DailyBrief(
        title=title,
        body=body,
        urgent_count=total_urgent,
        severity=severity,
        plant_id=plant_id_str,
        plant_name=plant_name,
        cta_prompt=cta_prompt,
        generated_at=now_iso,
        reason_tags=reason_tags,
    )
    await cache_brief(user_id, brief)
    return brief
