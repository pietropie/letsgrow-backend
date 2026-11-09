"""
Plano recomendado pelo uso real da conta (UX item 19b, GET /billing/recommendation).

Uma regra só para o app e o web:
- motivos (reasons): limite de plantas ou de ambientes atingido, consultas esgotadas
  ou perto do fim (near_limit do /usage) e créditos esgotados;
- candidatos: planos pagos acima do atual, à venda na plataforma pedida;
- com motivo: o menor candidato que aumenta todos os limites com motivo; senão o que
  aumenta mais deles (empate = o menor); se nenhum aumenta nenhum, o próximo à venda;
- sem motivo: o próximo à venda ("PRÓXIMO PASSO");
- sem candidato: nenhum plano.
"""
from __future__ import annotations

from typing import Literal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.grow import Grow
from app.models.plant import Plant
from app.models.user import User
from app.services import billing
from app.services import google_play as gp
from app.services import mercadopago as mp
from app.services.plan_config_service import get_all_plan_configs
from app.services.subscription import _effective_plan, _plan_limits
from app.services.usage import get_usage

Platform = Literal["android", "web"]

PLAN_ORDER = ("free", "jardineiro", "cultivador", "grower_pro")

# Motivo -> campo do plano que precisa aumentar para cobri-lo
_REASON_FIELD = {
    "plants_limit": "max_plants",
    "grows_limit": "max_grows",
    "queries_limit": "ai_queries_per_month",
    "queries_near_limit": "ai_queries_per_month",
    "credits_exhausted": "monthly_credits",
}


def _unlimited(value):
    """None (ou o 999999 legado) = sem limite."""
    return None if value is None or value >= 999999 else value


def _greater(new, old) -> bool:
    """O limite novo é maior? None = sem limite, maior que qualquer número."""
    if old is None:
        return False
    if new is None:
        return True
    return new > old


def _limits(plan_key: str, configs: dict[str, dict]) -> dict:
    cfg = configs.get(plan_key)
    base = _plan_limits(plan_key)
    return {
        "max_plants": base["max_plants"],
        "max_grows": base["max_grows"],
        "max_pots_per_grow": base["max_pots_per_grow"],
        "ai_queries_per_month": _unlimited(base["ai_queries_per_month"]),
        "monthly_credits": (cfg or {}).get("monthly_credits") or 0,
        "sensors_allowed": bool(base["sensors_allowed"]),
    }


def _sellable_on(platform: Platform, price) -> bool:
    if not billing.is_sellable(price):
        return False
    if platform == "web":
        return mp.is_configured()
    return gp.is_configured() and bool(price.google_product_id) and bool(price.google_base_plan_id)


async def _for_sale(db: AsyncSession, platform: Platform) -> set[str]:
    return {p.plan_key for p in await billing.plan_prices(db) if _sellable_on(platform, p)}


async def _counts(db: AsyncSession, user: User) -> tuple[int, int]:
    """As mesmas contagens das travas de criação (plantas ativas, ambientes ativos)."""
    plants = (
        await db.execute(
            select(func.count(Plant.id)).where(Plant.user_id == user.id, Plant.is_active.is_(True))
        )
    ).scalar_one()
    grows = (
        await db.execute(select(func.count(Grow.id)).where(Grow.user_id == user.id, Grow.status == "active"))
    ).scalar_one()
    return int(plants), int(grows)


def _reasons(usage: dict, current: dict, plants: int, grows: int) -> list[str]:
    reasons: list[str] = []
    if plants >= current["max_plants"]:
        reasons.append("plants_limit")
    if grows >= current["max_grows"]:
        reasons.append("grows_limit")
    queries = usage.get("queries")
    if queries:
        if queries["remaining"] <= 0:
            reasons.append("queries_limit")
        elif queries["total"] > 0 and queries["used"] / queries["total"] >= usage["warning_threshold"]:
            reasons.append("queries_near_limit")
    credits = usage.get("credits")
    if credits and credits["remaining"] <= 0:
        reasons.append("credits_exhausted")
    return reasons


def _covers(reasons: list[str], current: dict, candidate: dict) -> list[str]:
    return [r for r in reasons if _greater(candidate[_REASON_FIELD[r]], current[_REASON_FIELD[r]])]


def _changes(usage: dict, current: dict, target: dict) -> list[dict]:
    rows = [
        ("plants", "max_plants"),
        ("grows", "max_grows"),
        ("pots_per_grow", "max_pots_per_grow"),
    ]
    if usage["mode"] != "credits":
        rows.append(("queries", "ai_queries_per_month"))
    if usage.get("credits") is not None:
        rows.append(("credits", "monthly_credits"))
    rows.append(("sensors", "sensors_allowed"))
    return [{"key": key, "from": current[field], "to": target[field]} for key, field in rows]


async def get_recommendation(db: AsyncSession, user: User, platform: Platform) -> dict:
    configs = await get_all_plan_configs(db)  # também aquece o cache de _plan_limits
    usage = await get_usage(db, user)
    plants, grows = await _counts(db, user)

    current_key = _effective_plan(user)
    current = _limits(current_key, configs)
    reasons = _reasons(usage, current, plants, grows)

    rank = PLAN_ORDER.index(current_key) if current_key in PLAN_ORDER else 0
    for_sale = await _for_sale(db, platform)
    candidates = [key for key in PLAN_ORDER[rank + 1:] if key in for_sale]

    chosen: str | None = None
    covers: list[str] = []
    if candidates:
        chosen = candidates[0]
        if reasons:
            best = -1
            for key in candidates:  # do menor para o maior: o empate fica com o menor
                hit = _covers(reasons, current, _limits(key, configs))
                if len(hit) > best:
                    chosen, covers, best = key, hit, len(hit)
                if best == len(reasons):
                    break

    return {
        "current_plan": current_key,
        "plan_key": chosen,
        "reasons": reasons,
        "covers": covers,
        "usage": {
            "plants": {"used": plants, "total": current["max_plants"]},
            "grows": {"used": grows, "total": current["max_grows"]},
        },
        "changes": _changes(usage, current, _limits(chosen, configs)) if chosen else [],
    }
