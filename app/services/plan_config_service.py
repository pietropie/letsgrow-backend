"""
Servico que le/escreve configuracoes de plano do banco de dados.

Cache em memoria (TTL 60s) para evitar query a cada requisicao.
Na primeira leitura, se a tabela estiver vazia, faz seed a partir de config.py.
"""
import asyncio
import time
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.plan_config import PlanConfig

# ── Cache em memoria ──────────────────────────────────────────────────────────

_cache: dict[str, dict] | None = None
_cache_ts: float = 0
_CACHE_TTL = 60  # segundos
_lock = asyncio.Lock()


def _invalidate() -> None:
    global _cache, _cache_ts
    _cache = None
    _cache_ts = 0


# ── Seed defaults ─────────────────────────────────────────────────────────────

SEED_DEFAULTS = [
    {
        "plan_key": "free",
        "label": "Free",
        "price_brl": 0.0,
        "price_display": "R$ 0",
        "period_display": "para sempre",
        "badge_text": None,
        "max_plants": settings.FREE_MAX_PLANTS,
        "max_grows": settings.FREE_MAX_GROWS,
        "max_pots_per_grow": settings.FREE_MAX_POTS_PER_GROW,
        "ai_queries_per_month": settings.FREE_AI_QUERIES_PER_MONTH,
        "sensors_allowed": False,
    },
    {
        "plan_key": "jardineiro",
        "label": "Jardineiro",
        "price_brl": 24.90,
        "price_display": "R$ 24,90",
        "period_display": "por mes",
        "badge_text": None,
        "max_plants": settings.JARDINEIRO_MAX_PLANTS,
        "max_grows": settings.JARDINEIRO_MAX_GROWS,
        "max_pots_per_grow": settings.JARDINEIRO_MAX_POTS_PER_GROW,
        "ai_queries_per_month": settings.JARDINEIRO_AI_QUERIES_PER_MONTH,
        "sensors_allowed": False,
    },
    {
        "plan_key": "cultivador",
        "label": "Cultivador",
        "price_brl": 44.90,
        "price_display": "R$ 44,90",
        "period_display": "por mes",
        "badge_text": "Popular",
        "max_plants": settings.CULTIVADOR_MAX_PLANTS,
        "max_grows": settings.CULTIVADOR_MAX_GROWS,
        "max_pots_per_grow": settings.CULTIVADOR_MAX_POTS_PER_GROW,
        "ai_queries_per_month": None,
        "sensors_allowed": True,
    },
    {
        "plan_key": "grower_pro",
        "label": "Grower Pro",
        "price_brl": 89.90,
        "price_display": "R$ 89,90",
        "period_display": "por mes",
        "badge_text": "Sem limites",
        "max_plants": settings.PRO_MAX_PLANTS,
        "max_grows": settings.PRO_MAX_GROWS,
        "max_pots_per_grow": settings.PRO_MAX_POTS_PER_GROW,
        "ai_queries_per_month": None,
        "sensors_allowed": True,
    },
]


async def _seed(db: AsyncSession) -> None:
    for row in SEED_DEFAULTS:
        db.add(PlanConfig(id=uuid.uuid4(), **row))
    await db.commit()


# ── API publica ───────────────────────────────────────────────────────────────

async def get_all_plan_configs(db: AsyncSession) -> dict[str, dict]:
    """Retorna dict plan_key -> campos. Usa cache de 60s."""
    global _cache, _cache_ts

    async with _lock:
        if _cache is not None and (time.monotonic() - _cache_ts) < _CACHE_TTL:
            return _cache

        rows = (await db.execute(select(PlanConfig).order_by(PlanConfig.created_at))).scalars().all()

        if not rows:
            await _seed(db)
            rows = (await db.execute(select(PlanConfig).order_by(PlanConfig.created_at))).scalars().all()

        _cache = {r.plan_key: _row_to_dict(r) for r in rows}
        _cache_ts = time.monotonic()
        return _cache


async def get_plan_config(db: AsyncSession, plan_key: str) -> dict | None:
    configs = await get_all_plan_configs(db)
    return configs.get(plan_key)


async def update_plan_config(db: AsyncSession, plan_key: str, updates: dict) -> dict | None:
    row = (
        await db.execute(select(PlanConfig).where(PlanConfig.plan_key == plan_key))
    ).scalar_one_or_none()

    if row is None:
        return None

    for field, value in updates.items():
        setattr(row, field, value)

    await db.commit()
    await db.refresh(row)
    _invalidate()
    return _row_to_dict(row)


def _row_to_dict(row: PlanConfig) -> dict:
    return {
        "plan_key": row.plan_key,
        "label": row.label,
        "price_brl": float(row.price_brl),
        "price_display": row.price_display,
        "period_display": row.period_display,
        "badge_text": row.badge_text,
        "max_plants": row.max_plants,
        "max_grows": row.max_grows,
        "max_pots_per_grow": row.max_pots_per_grow,
        "ai_queries_per_month": row.ai_queries_per_month,
        "sensors_allowed": row.sensors_allowed,
        "updated_by": row.updated_by,
    }


def get_limits_from_cache(plan_key: str) -> dict | None:
    """Leitura sincrona do cache — retorna None se o cache ainda nao foi populado."""
    if _cache is None:
        return None
    return _cache.get(plan_key)
