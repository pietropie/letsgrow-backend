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
from datetime import date, datetime, timedelta, timezone
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import GrowEvent
from app.models.plant import Plant
from app.services import ai_provider
from app.services.bob_accountability import upsert_actionable_recommendations
from app.services.bob_env import load_latest_env
from app.services.bob_rules import BobStep, compute_bob_steps
from app.services.redis_client import _get_redis

logger = logging.getLogger(__name__)

_BRIEF_TTL = 86_400  # 24 horas
_BRAZIL_TZ = timezone(timedelta(hours=-3))

Severity = Literal["ok", "attention", "urgent"]


@dataclass
class DailyBrief:
    title: str                          # ex: "🚨 Bob: Gelato precisa de ação agora"
    body: str                           # texto gerado pelo LLM (2-3 frases)
    urgent_count: int                   # quantas ações urgentes (para priorizar push)
    severity: Severity = "ok"           # ok | attention | urgent — controla cor do card
    plant_id: str | None = None         # UUID da planta mais relevante (str para JSON)
    plant_name: str | None = None       # nome da strain mais relevante
    cta_prompt: str = ""                # pergunta pré-preenchida para abrir o Bob
    generated_at: str = ""             # ISO UTC timestamp da geração
    reason_tags: list[str] = field(default_factory=list)  # ["rega","floração","flush"]
    has_anomaly: bool = False           # True se tier=="anomaly" em qualquer planta


# ─── Cache Redis (JSON único por usuário) ─────────────────────────────────────

def _cache_key(user_id: uuid.UUID) -> str:
    return f"daily_brief_v2:{user_id}"


async def get_cached_brief(user_id: uuid.UUID) -> DailyBrief | None:
    try:
        r = _get_redis()
        raw = await r.get(_cache_key(user_id))
        if raw:
            data = json.loads(raw)
            return DailyBrief(
                title=data["title"],
                body=data["body"],
                urgent_count=data["urgent_count"],
                severity=data.get("severity", "ok"),
                plant_id=data.get("plant_id"),
                plant_name=data.get("plant_name"),
                cta_prompt=data.get("cta_prompt", ""),
                generated_at=data.get("generated_at", ""),
                reason_tags=data.get("reason_tags", []),
                has_anomaly=data.get("has_anomaly", False),
            )
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
            "has_anomaly": brief.has_anomaly,
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

async def _load_plant_steps(db: AsyncSession, plant: Plant, today: date) -> list[BobStep]:
    """Carrega últimos 15 eventos e retorna BobSteps do motor unificado."""
    ev_result = await db.execute(
        select(GrowEvent)
        .where(GrowEvent.plant_id == plant.id)
        .order_by(GrowEvent.event_date.desc())
        .limit(15)
    )
    events = ev_result.scalars().all()
    latest_temp, latest_humidity = await load_latest_env(db, plant.id)
    return await compute_bob_steps(
        plant,
        list(events),
        today,
        latest_temp=latest_temp,
        latest_humidity=latest_humidity,
    )


# ─── Extração de reason_tags ──────────────────────────────────────────────────

_PHASE_TAGS: dict[str, str] = {
    "germination": "germinação",
    "seedling": "mudas",
    "veg": "vegetativo",
    "flower": "floração",
    "harvest": "colheita",
}

_ACTION_TO_TAG: dict[str, str] = {
    "watering": "rega",
    "flush": "flush",
    "harvest": "colheita",
    "topping": "poda",
    "lst": "treinamento",
    "desfolha": "desfolha",
    "mkp": "nutrição",
    "flip": "floração",
}


def _extract_reason_tags(
    plant_steps: list[tuple[str, list[BobStep]]],
    plants: list[Plant],
) -> list[str]:
    """Deriva tags legíveis a partir dos steps (via step.action) + fases das plantas."""
    tags: list[str] = []
    seen: set[str] = set()

    for _, steps in plant_steps:
        for step in steps:
            tag = _ACTION_TO_TAG.get(step.action)
            if tag and tag not in seen:
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

_BRIEF_SYSTEM = """Você é o Bob, companion de cultivo do Let's Grow.
Gere um briefing diário CURTÍSSIMO (2-3 frases) para o cultivador.
- Priorize a ação mais urgente (🔴 crítico > 🟠 anomalia > 🟡 atenção)
- Se há algo crítico, seja direto: "HOJE você precisa..." — sem rodeios
- Explique brevemente o motivo em linguagem simples
- Tom WhatsApp: sem lista, sem formalidade, máx 50 palavras
- Termine com pergunta ou convite à ação"""

_TIER_PREFIX: dict[str, str] = {
    "critical": "🔴",
    "anomaly": "🟠",
    "attention": "🟡",
    "info": "🟢",
}


async def _generate_brief_text(plant_steps: list[tuple[str, list[BobStep]]]) -> str:
    lines: list[str] = []
    for strain_name, steps in plant_steps:
        if steps:
            lines.append(f"{strain_name}:")
            for step in steps[:2]:  # top 2 por planta
                prefix = _TIER_PREFIX.get(step.tier, "-")
                lines.append(f"  {prefix} {step.message}")

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
                return steps[0].message
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

    today = datetime.now(_BRAZIL_TZ).date()
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

    # Computa ações por planta e persiste recomendações acionáveis (dedup)
    plant_steps: list[tuple[str, list[BobStep]]] = []
    for plant in plants:
        steps = await _load_plant_steps(db, plant, today)
        plant_steps.append((plant.strain_name or "Planta", steps))
        try:
            await upsert_actionable_recommendations(db, user_id, plant, steps)
        except Exception as exc:
            logger.warning("Bob: falha ao persistir recomendacoes do brief: %s", exc)
    try:
        await db.commit()
    except Exception as exc:
        logger.warning("Bob: commit de recomendacoes do brief falhou: %s", exc)
        await db.rollback()

    # Contagem de tiers para severity
    all_steps: list[BobStep] = [s for _, steps in plant_steps for s in steps]
    n_critical = sum(1 for s in all_steps if s.tier == "critical")
    n_anomaly = sum(1 for s in all_steps if s.tier == "anomaly")
    has_any_steps = bool(all_steps)

    if n_critical > 0:
        severity: Severity = "urgent"
    elif n_anomaly > 0 or has_any_steps:
        severity = "attention"
    else:
        severity = "ok"

    has_anomaly = n_anomaly > 0

    # Coletar todas as plantas críticas
    critical_plants: list[Plant] = [
        plant
        for plant, (_, steps) in zip(plants, plant_steps)
        if any(s.tier == "critical" for s in steps)
    ]

    # Planta principal (primeira crítica ou primeira da lista)
    main_plant = critical_plants[0] if critical_plants else plants[0]
    plant_id_str = str(main_plant.id)
    plant_name = main_plant.strain_name or "Planta"

    # Tags
    reason_tags = _extract_reason_tags(plant_steps, list(plants))

    # Gera texto via LLM
    body = await _generate_brief_text(plant_steps)

    # Título dinâmico
    if len(critical_plants) > 1:
        title = f"🚨 Bob: {len(critical_plants)} plantas precisam de ação hoje"
    elif len(critical_plants) == 1:
        title = f"🚨 Bob: {critical_plants[0].strain_name} precisa de ação HOJE"
    elif severity == "attention":
        title = "🌿 Bob: tem coisas para cuidar hoje"
    else:
        title = "✅ Bob: tudo em dia no cultivo!"

    # CTA prompt
    cta_prompt = _build_cta_prompt(severity, plant_name, reason_tags)

    brief = DailyBrief(
        title=title,
        body=body,
        urgent_count=n_critical,
        severity=severity,
        plant_id=plant_id_str,
        plant_name=plant_name,
        cta_prompt=cta_prompt,
        generated_at=now_iso,
        reason_tags=reason_tags,
        has_anomaly=has_anomaly,
    )
    await cache_brief(user_id, brief)
    return brief
