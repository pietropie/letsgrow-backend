from datetime import datetime, timezone

from fastapi import HTTPException, status

from app.config import settings
from app.models.user import User


def _plan_limits(plan: str) -> dict:
    if plan == "pro":
        return {
            "max_grows": settings.PRO_MAX_GROWS,
            "max_pots_per_grow": settings.PRO_MAX_POTS_PER_GROW,
            "ai_queries_per_month": 999999,
        }
    if plan == "grower":
        return {
            "max_grows": settings.GROWER_MAX_GROWS,
            "max_pots_per_grow": settings.GROWER_MAX_POTS_PER_GROW,
            "ai_queries_per_month": 999999,
        }
    # free
    return {
        "max_grows": settings.FREE_MAX_GROWS,
        "max_pots_per_grow": settings.FREE_MAX_POTS_PER_GROW,
        "ai_queries_per_month": settings.FREE_AI_QUERIES_PER_MONTH,
    }


def _effective_plan(user: User) -> str:
    if user.plan in ("grower", "pro"):
        if user.plan_expires_at and user.plan_expires_at < datetime.now(timezone.utc):
            return "free"
    return user.plan


def check_grow_limit(user: User, current_grow_count: int) -> None:
    plan = _effective_plan(user)
    limits = _plan_limits(plan)
    if current_grow_count >= limits["max_grows"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Limite de grows atingido para o plano {plan.upper()}. Faça upgrade para continuar.",
        )


def check_pot_limit(user: User, current_pot_count: int) -> None:
    plan = _effective_plan(user)
    limits = _plan_limits(plan)
    if current_pot_count >= limits["max_pots_per_grow"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Limite de vasos por grow atingido para o plano {plan.upper()}.",
        )


def check_ai_limit(user: User) -> None:
    plan = _effective_plan(user)
    limits = _plan_limits(plan)
    if plan == "free" and user.ai_queries_this_month >= limits["ai_queries_per_month"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Limite de consultas IA atingido este mês. Faça upgrade para o plano Grower.",
        )
