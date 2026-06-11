"""
Controle de limites de plano.

Os limites agora vem do banco (tabela plan_configs), editavel via admin.
Fallback para config.py se o cache ainda nao tiver sido populado.

Planos: free | jardineiro | cultivador | grower_pro
Legado (mantido por compatibilidade): grower -> cultivador, pro -> grower_pro
Expiracao: se plan_expires_at < agora, o plano pago e tratado como free.
"""
from datetime import datetime, timezone

from fastapi import HTTPException, status

from app.config import settings
from app.models.user import User

PAID_PLANS = ("jardineiro", "cultivador", "grower_pro", "grower", "pro")

# Normaliza planos legados
_LEGACY: dict[str, str] = {"grower": "cultivador", "pro": "grower_pro"}

# Fallback estatico (usado quando o cache do plan_config_service ainda nao existe)
_STATIC_FALLBACK: dict[str, dict] = {
    "free":       {"max_grows": settings.FREE_MAX_GROWS,        "max_pots_per_grow": settings.FREE_MAX_POTS_PER_GROW,        "max_plants": settings.FREE_MAX_PLANTS,        "ai_queries_per_month": settings.FREE_AI_QUERIES_PER_MONTH,        "sensors_allowed": False},
    "jardineiro": {"max_grows": settings.JARDINEIRO_MAX_GROWS,  "max_pots_per_grow": settings.JARDINEIRO_MAX_POTS_PER_GROW,  "max_plants": settings.JARDINEIRO_MAX_PLANTS,  "ai_queries_per_month": settings.JARDINEIRO_AI_QUERIES_PER_MONTH,  "sensors_allowed": False},
    "cultivador": {"max_grows": settings.CULTIVADOR_MAX_GROWS,  "max_pots_per_grow": settings.CULTIVADOR_MAX_POTS_PER_GROW,  "max_plants": settings.CULTIVADOR_MAX_PLANTS,  "ai_queries_per_month": None,                                      "sensors_allowed": True},
    "grower_pro": {"max_grows": settings.PRO_MAX_GROWS,         "max_pots_per_grow": settings.PRO_MAX_POTS_PER_GROW,         "max_plants": settings.PRO_MAX_PLANTS,         "ai_queries_per_month": None,                                      "sensors_allowed": True},
}


def _effective_plan(user: User) -> str:
    plan = user.plan
    if plan in _LEGACY:
        plan = _LEGACY[plan]
    if plan in PAID_PLANS:
        if user.plan_expires_at and user.plan_expires_at < datetime.now(timezone.utc):
            return "free"
    return plan


def _plan_limits(plan_key: str) -> dict:
    """Le limites do cache do plan_config_service; cai no fallback estatico."""
    from app.services.plan_config_service import get_limits_from_cache
    cached = get_limits_from_cache(plan_key)
    if cached is not None:
        ai = cached["ai_queries_per_month"]
        return {
            "max_grows": cached["max_grows"],
            "max_pots_per_grow": cached["max_pots_per_grow"],
            "max_plants": cached["max_plants"],
            "ai_queries_per_month": ai if ai is not None else 999999,
            "sensors_allowed": cached["sensors_allowed"],
        }
    # Fallback
    fb = _STATIC_FALLBACK.get(plan_key, _STATIC_FALLBACK["free"])
    return {**fb, "ai_queries_per_month": fb["ai_queries_per_month"] if fb["ai_queries_per_month"] is not None else 999999}


# ── Checks ────────────────────────────────────────────────────────────────────

def check_grow_limit(user: User, current_grow_count: int) -> None:
    plan = _effective_plan(user)
    limits = _plan_limits(plan)
    if current_grow_count >= limits["max_grows"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Limite de grows atingido para o plano {plan.upper()}. Faca upgrade para continuar.",
        )


def check_pot_limit(user: User, current_pot_count: int) -> None:
    plan = _effective_plan(user)
    limits = _plan_limits(plan)
    if current_pot_count >= limits["max_pots_per_grow"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Limite de vasos por grow atingido para o plano {plan.upper()}.",
        )


def check_plant_limit(user: User, current_plant_count: int) -> None:
    plan = _effective_plan(user)
    limits = _plan_limits(plan)
    if current_plant_count >= limits["max_plants"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Limite de plantas atingido para o plano {plan.upper()} "
                f"({limits['max_plants']} plantas). Faca upgrade para continuar."
            ),
        )


def check_ai_limit(user: User) -> None:
    plan = _effective_plan(user)
    limits = _plan_limits(plan)
    ai_limit = limits["ai_queries_per_month"]
    if ai_limit < 999999 and user.ai_queries_this_month >= ai_limit:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Limite de {ai_limit} consultas de IA atingido este mes. "
                f"Faca upgrade para continuar usando o Bob."
            ),
        )


def check_sensor_access(user: User) -> None:
    plan = _effective_plan(user)
    limits = _plan_limits(plan)
    if not limits["sensors_allowed"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sensores disponiveis a partir do plano Cultivador. Faca upgrade para ativar.",
        )


# ── Status do plano ───────────────────────────────────────────────────────────

def get_plan_status(user: User, current_plant_count: int, current_grow_count: int) -> dict:
    effective = _effective_plan(user)
    limits = _plan_limits(effective)

    is_expired = (
        user.plan in PAID_PLANS
        and user.plan_expires_at is not None
        and user.plan_expires_at < datetime.now(timezone.utc)
    )

    ai_limit = limits["ai_queries_per_month"]

    return {
        "plan": user.plan,
        "effective_plan": effective,
        "is_expired": is_expired,
        "plan_expires_at": user.plan_expires_at,
        "max_plants": limits["max_plants"],
        "max_grows": limits["max_grows"],
        "max_pots_per_grow": limits["max_pots_per_grow"],
        "ai_queries_per_month": ai_limit if ai_limit < 999999 else None,
        "sensors_allowed": limits["sensors_allowed"],
        "plants_used": current_plant_count,
        "grows_used": current_grow_count,
        "ai_queries_used": user.ai_queries_this_month,
        "ai_queries_reset_at": user.ai_queries_reset_at,
    }
