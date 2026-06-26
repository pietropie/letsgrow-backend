"""
Serviço de segmentação de usuários para push notifications.

Cada filtro é opcional — apenas os preenchidos são aplicados (AND entre eles).
Todos os filtros só consideram usuários ativos com expo_push_token registrado.

Filtros disponíveis:
  plans                  — Plano do cliente (ex: ["free", "pro"])
  plant_phases           — Fase de alguma planta ativa (ex: ["flower", "veg"])
  harvest_within_days    — Colheita nos últimos N dias (plant.harvest_date)
  has_sensor             — Tem pelo menos 1 SensorDevice associado a uma planta
  account_anniversary    — Aniversário de conta hoje (mês + dia de created_at)
  downgraded_within_days — Voltou para free nos últimos N dias (plan_expires_at recente)
  inactive_for_days      — Não abre o app há N dias (last_seen_at)
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel
from sqlalchemy import exists, extract, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.plant import Plant
from app.models.sensor import SensorDevice
from app.models.user import User

logger = logging.getLogger(__name__)


# ── Schema de filtros ─────────────────────────────────────────────────────────

class SegmentFilters(BaseModel):
    """Todos os campos são opcionais. Filtros não preenchidos são ignorados."""

    plans: list[str] | None = None
    """Planos a incluir, ex: ["free", "pro"]. None = todos os planos."""

    plant_phases: list[str] | None = None
    """Fases de planta, ex: ["flower", "veg"]. Usuário deve ter ≥1 planta ativa nessa fase."""

    harvest_within_days: int | None = None
    """Usuário com ≥1 planta colhida nos últimos N dias."""

    has_sensor: bool | None = None
    """True → usuário tem ≥1 SensorDevice vinculado a alguma planta sua."""

    account_anniversary: bool | None = None
    """True → aniversário de conta hoje (mês+dia de created_at == hoje)."""

    downgraded_within_days: int | None = None
    """Voltou para free nos últimos N dias (plan='free' e plan_expires_at >= now-N)."""

    inactive_for_days: int | None = None
    """Não abre o app há ≥N dias (last_seen_at <= now-N ou null)."""


# ── Construção da query ───────────────────────────────────────────────────────

async def evaluate_segment(
    db: AsyncSession,
    filters: SegmentFilters,
) -> list[User]:
    """
    Retorna lista de Users que satisfazem todos os filtros fornecidos.
    Sempre filtra: is_active=True e expo_push_token não nulo.
    """
    now = datetime.now(timezone.utc)
    today = date.today()

    q = select(User).where(
        User.is_active == True,  # noqa: E712
    )

    # ── Plano ────────────────────────────────────────────────────────────────
    if filters.plans:
        q = q.where(User.plan.in_(filters.plans))

    # ── Fase da planta ───────────────────────────────────────────────────────
    if filters.plant_phases:
        phase_sub = (
            select(Plant.id)
            .where(
                Plant.user_id == User.id,
                Plant.is_active == True,  # noqa: E712
                Plant.current_phase.in_(filters.plant_phases),
            )
            .correlate(User)
            .exists()
        )
        q = q.where(phase_sub)

    # ── Colheita recente ─────────────────────────────────────────────────────
    if filters.harvest_within_days is not None:
        cutoff = today - timedelta(days=filters.harvest_within_days)
        harvest_sub = (
            select(Plant.id)
            .where(
                Plant.user_id == User.id,
                Plant.harvest_date.isnot(None),
                Plant.harvest_date >= cutoff,
            )
            .correlate(User)
            .exists()
        )
        q = q.where(harvest_sub)

    # ── Tem sensor ───────────────────────────────────────────────────────────
    if filters.has_sensor is True:
        sensor_sub = (
            select(SensorDevice.id)
            .join(Plant, Plant.id == SensorDevice.plant_id)
            .where(Plant.user_id == User.id)
            .correlate(User)
            .exists()
        )
        q = q.where(sensor_sub)

    # ── Aniversário de conta ─────────────────────────────────────────────────
    if filters.account_anniversary is True:
        q = q.where(
            extract("month", User.created_at) == today.month,
            extract("day", User.created_at) == today.day,
        )

    # ── Downgrade recente (voltou para free) ─────────────────────────────────
    if filters.downgraded_within_days is not None:
        cutoff_dt = now - timedelta(days=filters.downgraded_within_days)
        q = q.where(
            User.plan == "free",
            User.plan_expires_at.isnot(None),
            User.plan_expires_at >= cutoff_dt,
        )

    # ── Inativo há N dias ────────────────────────────────────────────────────
    if filters.inactive_for_days is not None:
        cutoff_dt = now - timedelta(days=filters.inactive_for_days)
        q = q.where(
            (User.last_seen_at.is_(None)) | (User.last_seen_at <= cutoff_dt)
        )

    result = await db.execute(q)
    users = result.scalars().all()
    logger.info("Segment evaluated: %d users matched (filters=%s)", len(users), filters.model_dump(exclude_none=True))
    return list(users)


async def preview_segment(
    db: AsyncSession,
    filters: SegmentFilters,
    sample_size: int = 5,
) -> dict[str, Any]:
    """
    Retorna contagem total de usuários que atendem os filtros,
    quantos deles têm push token (notificáveis), e uma amostra de e-mails.
    """
    users = await evaluate_segment(db, filters)
    pushable = [u for u in users if u.expo_push_token]
    sample = [u.email for u in users[:sample_size]]
    return {
        "count": len(users),
        "pushable_count": len(pushable),
        "sample_emails": sample,
    }
