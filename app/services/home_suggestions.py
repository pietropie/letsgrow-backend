"""
Sugestões do Bobby para o Início e a próxima ação de cada planta (UX item 20).

Tudo sai do que a conta registrou, sem LLM e sem gastar conversa:
- itens de atenção do motor de regras (rega atrasada, temperatura, colheita...);
- fase e dia da fase, só com a data cadastrada ou o registro de FASE;
- a última leitura do ambiente (manual ou sensor) com até 24 h.

Sem contexto, a lista volta vazia. Cada sugestão traz `basis`, com o motivo e os
registros que a sustentam, para o app mostrar de onde ela veio.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.event import GrowEvent
from app.models.grow import Grow
from app.models.plant import Plant
from app.services.bob_env import measurement_evidence
from app.services.bob_rules import BobStep, _plant_evidence, compute_bob_steps, event_evidence
from app.services.chat_sources import EVENT_LABELS, _number
from app.services.grow_env import latest_grow_measurements

MAX_SUGGESTIONS = 3
READING_MAX_AGE = timedelta(hours=24)
_BRAZIL_TZ = timezone(timedelta(hours=-3))

# Itens de atenção: o que o app mostra em "Precisa de atenção" e no cartão da planta
ATTENTION_TIERS = ("critical", "anomaly", "attention")
_TIER_RANK = {"critical": 0, "anomaly": 1, "attention": 2, "info": 3}

PHASE_LABELS = {
    "germination": "Germinação", "seedling": "Muda", "veg": "Vegetativo", "flower": "Floração",
    "late_flower": "Floração final", "harvest": "Colheita", "drying": "Secagem", "curing": "Cura",
}
# Registro de FASE que marca o início de cada fase (mesma regra do app, lib/plantFacts.ts)
_PHASE_START_EVENT = {
    "germination": "germination", "seedling": "seedling", "veg": "veg",
    "flower": "flip", "late_flower": "flip", "harvest": "harvest",
}
ACTION_TOPICS = {
    "watering": "Rega", "flush": "Flush", "harvest": "Colheita", "tricomas": "Tricomas", "topping": "Topping",
    "lst": "Treinamento", "desfolha": "Desfolha", "mkp": "Nutrição", "flip": "Virada para floração",
    "temperature": "Temperatura", "humidity": "Umidade",
}


@dataclass
class PlantContext:
    plant: Plant
    name: str
    events: list[GrowEvent]
    steps: list[BobStep]
    phase_day: int | None
    phase_start_evidence: dict | None


def plant_name(plant: Plant) -> str:
    return plant.display_name or plant.strain_name or "Planta"


def _phase_start(plant: Plant, events: list[GrowEvent]) -> tuple[date | None, dict | None]:
    own = {"germination": plant.germination_date, "flower": plant.flip_date,
           "late_flower": plant.flip_date, "harvest": plant.harvest_date}.get(plant.current_phase)
    if own is not None:
        return own, _plant_evidence(plant)
    start_type = _PHASE_START_EVENT.get(plant.current_phase)
    for ev in events:  # mais recentes primeiro
        if start_type and ev.event_type == start_type:
            when = ev.event_date
            if isinstance(when, datetime):  # o dia do registro no horário de Brasília, como o "hoje"
                when = (when if when.tzinfo else when.replace(tzinfo=timezone.utc)).astimezone(_BRAZIL_TZ).date()
            return when, event_evidence(ev)
    return None, None


async def load_plant_context(db: AsyncSession, plant: Plant, today: date) -> PlantContext:
    from app.services.bob_env import load_latest_env_with_evidence

    events = list((
        await db.execute(
            select(GrowEvent).where(GrowEvent.plant_id == plant.id).order_by(GrowEvent.event_date.desc()).limit(15)
        )
    ).scalars().all())
    latest_temp, latest_humidity, env_evidence = await load_latest_env_with_evidence(db, plant.id, plant.grow_id)
    steps = await compute_bob_steps(
        plant, events, today,
        latest_temp=latest_temp, latest_humidity=latest_humidity, env_evidence=env_evidence,
    )
    start, start_evidence = _phase_start(plant, events)
    day = max(0, (today - start).days) + 1 if start is not None else None  # o primeiro dia é o dia 1
    return PlantContext(plant, plant_name(plant), events, steps, day, start_evidence)


def next_action(ctx: PlantContext) -> dict | None:
    """O primeiro item de atenção da planta, ou None quando não há pendência."""
    pending = sorted((s for s in ctx.steps if s.tier in ATTENTION_TIERS), key=lambda s: _TIER_RANK[s.tier])
    if not pending:
        return None
    step = pending[0]
    return {"action": step.action, "tier": step.tier, "message": step.message, "evidence": step.evidence}


def last_event(ctx: PlantContext) -> dict | None:
    if not ctx.events:
        return None
    ev = ctx.events[0]
    return {"id": ev.id, "event_type": ev.event_type, "label": EVENT_LABELS.get(ev.event_type, "Registro"),
            "event_date": ev.event_date}


def _phase_text(ctx: PlantContext) -> str | None:
    phase = PHASE_LABELS.get(ctx.plant.current_phase)
    if phase is None:
        return None
    return f"{phase}, dia {ctx.phase_day}" if ctx.phase_day else phase


def _step_suggestion(ctx: PlantContext, step: BobStep) -> dict:
    topic = ACTION_TOPICS.get(step.action, "Atenção")
    return {
        "label": f"{topic} · {ctx.name}",
        "prompt": f"Sobre {ctx.name}: {step.message} O que você recomenda fazer agora?",
        "plant_id": ctx.plant.id,
        "grow_id": ctx.plant.grow_id,
        "basis": {"kind": "attention", "text": step.message, "tier": step.tier, "evidence": step.evidence},
    }


def _phase_suggestion(ctx: PlantContext) -> dict | None:
    text = _phase_text(ctx)
    if text is None:
        return None
    phase = PHASE_LABELS[ctx.plant.current_phase].lower()
    when = f", dia {ctx.phase_day} da fase" if ctx.phase_day else ""
    return {
        "label": f"{ctx.name} · {text}",
        "prompt": f"{ctx.name} está em {phase}{when}. O que devo observar e fazer nesta etapa?",
        "plant_id": ctx.plant.id,
        "grow_id": ctx.plant.grow_id,
        "basis": {"kind": "phase", "text": f"Fase registrada: {text}", "tier": None,
                  "evidence": [ctx.phase_start_evidence or _plant_evidence(ctx.plant)]},
    }


async def _reading_suggestion(
    db: AsyncSession, ctx: PlantContext, grow_name: str, now: datetime,
) -> dict | None:
    readings = await latest_grow_measurements(db, ctx.plant.grow_id)
    fresh = {
        metric: m for metric, m in readings.items()
        if metric in ("temperature", "humidity") and m.value is not None and m.observed_at is not None
        and now - (m.observed_at if m.observed_at.tzinfo else m.observed_at.replace(tzinfo=timezone.utc)) <= READING_MAX_AGE
    }
    if not fresh:
        return None
    parts = []
    if "temperature" in fresh:
        parts.append(f"{_number(fresh['temperature'].value)} °C")
    if "humidity" in fresh:
        parts.append(f"{_number(fresh['humidity'].value)}% de umidade")
    sources = {m.source for m in fresh.values()}
    origin = "do sensor" if sources == {"sensor"} else "registrada"
    phase = _phase_text(ctx)
    for_whom = f"{ctx.name} ({phase.lower()})" if phase else ctx.name
    reading = " e ".join(parts)
    return {
        "label": f"Clima · {grow_name}",
        "prompt": f"Em {grow_name}, a última leitura {origin} foi {reading}. Isso está bom para {for_whom}?",
        "plant_id": ctx.plant.id,
        "grow_id": ctx.plant.grow_id,
        "basis": {"kind": "reading", "text": f"Última leitura em {grow_name}: {reading}", "tier": None,
                  "evidence": [measurement_evidence(metric, m, ctx.plant.grow_id) for metric, m in fresh.items()]},
    }


async def build_suggestions(db: AsyncSession, user_id: uuid.UUID, now: datetime | None = None) -> list[dict]:
    """Até 3 perguntas prontas para o Bobby, da mais urgente para a mais geral."""
    now = now or datetime.now(timezone.utc)
    today = now.astimezone(_BRAZIL_TZ).date()
    plants = list((
        await db.execute(
            select(Plant).where(Plant.user_id == user_id, Plant.is_active.is_(True))
            .order_by(Plant.created_at.desc()).limit(10)
        )
    ).scalars().all())
    if not plants:
        return []
    grow_names = dict((
        await db.execute(select(Grow.id, Grow.name).where(Grow.id.in_({p.grow_id for p in plants})))
    ).all())
    contexts = [await load_plant_context(db, plant, today) for plant in plants]

    # 1. Itens de atenção, do mais grave para o mais leve; uma ação por planta
    steps = sorted(
        ((ctx, step) for ctx in contexts for step in ctx.steps if step.tier in ATTENTION_TIERS),
        key=lambda pair: _TIER_RANK[pair[1].tier],
    )
    seen: set[tuple] = set()
    candidates: list[dict] = []
    for ctx, step in steps:
        if (ctx.plant.id, step.action) not in seen:
            seen.add((ctx.plant.id, step.action))
            candidates.append(_step_suggestion(ctx, step))
    # 2. Fase e dia (a primeira planta antes das leituras, para variar os assuntos)
    phases = [s for s in (_phase_suggestion(ctx) for ctx in contexts) if s]
    candidates += phases[:1]
    # 3. Última leitura recente, uma por ambiente
    grows_done: set[uuid.UUID] = set()
    for ctx in contexts:
        if len(candidates) >= MAX_SUGGESTIONS or ctx.plant.grow_id in grows_done:
            continue
        grows_done.add(ctx.plant.grow_id)
        suggestion = await _reading_suggestion(db, ctx, grow_names.get(ctx.plant.grow_id) or "Ambiente", now)
        if suggestion:
            candidates.append(suggestion)
    candidates += phases[1:]
    # 4. Dicas informativas do motor de regras, por último
    for ctx in contexts:
        for step in ctx.steps:
            if step.tier == "info" and (ctx.plant.id, step.action) not in seen:
                seen.add((ctx.plant.id, step.action))
                candidates.append(_step_suggestion(ctx, step))
    return candidates[:MAX_SUGGESTIONS]
