"""Persistencia e fechamento do loop de accountability do Bob.

Recomendacoes nascem das regras (BobStep), nunca do texto do LLM.
Um par (user, plant, action) so pode ter UMA recomendacao aberta.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.bob import BobRecommendation
from app.models.event import GrowEvent
from app.models.plant import Plant
from app.services.bob_rules import BobStep

logger = logging.getLogger(__name__)

_ACTIONABLE_TIERS = frozenset({"critical", "attention"})

_DEADLINE_HOURS = {
    "critical": 24,
    "attention": 48,
}

# Evento do diario → actions de recomendacao que ele cumpre
_EVENT_TO_ACTIONS: dict[str, tuple[str, ...]] = {
    "rega": ("watering",),
    "watering": ("watering",),
    "nutrients": ("watering", "mkp"),
    "feeding": ("watering", "mkp"),
    "flush": ("flush",),
    "topping": ("topping",),
    "lst": ("lst",),
    "training": ("lst",),
    "flip": ("flip",),
    "defoliation": ("desfolha",),
    "desfolha": ("desfolha",),
    "harvest": ("harvest",),
    "pruning": ("desfolha",),
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def upsert_actionable_recommendations(
    db: AsyncSession,
    user_id: uuid.UUID,
    plant: Plant,
    steps: Iterable[BobStep],
) -> BobRecommendation | None:
    """Garante uma recomendacao aberta por (user, plant, action).

    Retorna a de maior urgencia (critical primeiro) para o caller devolver
    no chat. Nao faz commit.
    """
    actionable = [s for s in steps if s.tier in _ACTIONABLE_TIERS]
    if not actionable:
        return None

    now = _now()
    existing_result = await db.execute(
        select(BobRecommendation).where(
            BobRecommendation.user_id == user_id,
            BobRecommendation.plant_id == plant.id,
            BobRecommendation.followed_up == False,  # noqa: E712
            BobRecommendation.deadline > now,
        )
    )
    open_by_action = {rec.action: rec for rec in existing_result.scalars().all()}

    ranked = sorted(actionable, key=lambda s: 0 if s.tier == "critical" else 1)
    top: BobRecommendation | None = None

    for step in ranked:
        rec = open_by_action.get(step.action)
        if rec is None:
            hours = _DEADLINE_HOURS.get(step.tier, 48)
            rec = BobRecommendation(
                user_id=user_id,
                plant_id=plant.id,
                plant_name=plant.strain_name or "Planta",
                action=step.action,
                recommended_at=now,
                deadline=now + timedelta(hours=hours),
                followed_up=False,
            )
            db.add(rec)
            open_by_action[step.action] = rec
        if top is None:
            top = rec

    if top is not None and top.id is None:
        await db.flush()

    return top


async def fulfill_matching_recommendations(
    db: AsyncSession,
    user_id: uuid.UUID,
    plant_id: uuid.UUID,
    event: GrowEvent,
) -> int:
    """Fecha recomendacoes abertas cobertas por um evento recem-salvo.

    Assim o diario (que ja existe no app) fecha o loop sem depender do modal.
    Nao faz commit. Retorna quantas recomendacoes foram cumpridas.
    """
    actions = list(_EVENT_TO_ACTIONS.get(event.event_type, ()))
    if getattr(event, "is_flush", False) and "flush" not in actions:
        actions.append("flush")
    if not actions:
        return 0

    result = await db.execute(
        select(BobRecommendation).where(
            BobRecommendation.user_id == user_id,
            BobRecommendation.plant_id == plant_id,
            BobRecommendation.followed_up == False,  # noqa: E712
            BobRecommendation.action.in_(actions),
        )
    )
    recs = list(result.scalars().all())
    if not recs:
        return 0

    fulfilled_at = event.event_date
    if isinstance(fulfilled_at, datetime) and fulfilled_at.tzinfo is None:
        fulfilled_at = fulfilled_at.replace(tzinfo=timezone.utc)

    for rec in recs:
        rec.followed_up = True
        rec.fulfilled_at = fulfilled_at

    logger.info(
        "Bob: %d recomendacao(oes) cumpridas via evento %s (plant=%s)",
        len(recs),
        event.event_type,
        plant_id,
    )
    return len(recs)


async def reschedule_recommendation(
    rec: BobRecommendation,
    *,
    hours: int = 48,
) -> None:
    """'Nao consegui' — mantem pendente e empurra o prazo."""
    rec.followed_up = False
    rec.fulfilled_at = None
    rec.deadline = _now() + timedelta(hours=hours)


async def run_event_side_effects(
    db: AsyncSession,
    plant: Plant,
    event: GrowEvent,
    user_id: uuid.UUID,
) -> None:
    """Anomalia + fecha recomendacoes cobertas. Nao faz commit. Nunca propaga erro."""
    try:
        from app.services.anomaly_detector import check_and_save_anomaly
        from app.services.rag import invalidate_plant_tip_cache

        ev_result = await db.execute(
            select(GrowEvent)
            .where(GrowEvent.plant_id == plant.id)
            .order_by(GrowEvent.event_date.desc())
            .limit(15)
        )
        recent_evs = ev_result.scalars().all()
        await check_and_save_anomaly(db, plant, recent_evs, user_id)
        await fulfill_matching_recommendations(db, user_id, plant.id, event)
        invalidate_plant_tip_cache(plant.id)
    except Exception as exc:
        logger.warning("Bob event hook falhou: %s", exc)
