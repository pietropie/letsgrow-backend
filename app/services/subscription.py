"""
Serviço de controle de limites de plano.

Todos os limites vêm de config.py (variáveis de ambiente) — sem tabela no
banco, sem redeploy para alterar. Para limites dinâmicos em runtime, migrar
para uma tabela `plans` no futuro.

Planos: free | grower | pro
Expiração: se plan_expires_at < agora, o plano pago é tratado como free.
"""
from datetime import datetime, timezone

from fastapi import HTTPException, status

from app.config import settings
from app.models.user import User


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _plan_limits(plan: str) -> dict:
    if plan == "pro":
        return {
            "max_grows": settings.PRO_MAX_GROWS,
            "max_pots_per_grow": settings.PRO_MAX_POTS_PER_GROW,
            "max_plants": settings.PRO_MAX_PLANTS,
            "ai_queries_per_month": 999999,
        }
    if plan == "grower":
        return {
            "max_grows": settings.GROWER_MAX_GROWS,
            "max_pots_per_grow": settings.GROWER_MAX_POTS_PER_GROW,
            "max_plants": settings.GROWER_MAX_PLANTS,
            "ai_queries_per_month": 999999,
        }
    # free (default)
    return {
        "max_grows": settings.FREE_MAX_GROWS,
        "max_pots_per_grow": settings.FREE_MAX_POTS_PER_GROW,
        "max_plants": settings.FREE_MAX_PLANTS,
        "ai_queries_per_month": settings.FREE_AI_QUERIES_PER_MONTH,
    }


def _effective_plan(user: User) -> str:
    """Retorna o plano real do usuário, revertendo para 'free' se expirado."""
    if user.plan in ("grower", "pro"):
        if user.plan_expires_at and user.plan_expires_at < datetime.now(timezone.utc):
            return "free"
    return user.plan


# ---------------------------------------------------------------------------
# Checks — lançam HTTPException 403 se limite atingido
# ---------------------------------------------------------------------------

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


def check_plant_limit(user: User, current_plant_count: int) -> None:
    plan = _effective_plan(user)
    limits = _plan_limits(plan)
    if current_plant_count >= limits["max_plants"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Limite de plantas atingido para o plano {plan.upper()} "
                f"({limits['max_plants']} plantas). Faça upgrade para continuar."
            ),
        )


def check_ai_limit(user: User) -> None:
    plan = _effective_plan(user)
    limits = _plan_limits(plan)
    if plan == "free" and user.ai_queries_this_month >= limits["ai_queries_per_month"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Limite de {limits['ai_queries_per_month']} consultas IA atingido este mês. "
                "Faça upgrade para o plano Grower."
            ),
        )


# ---------------------------------------------------------------------------
# Status do plano — usado pelo endpoint GET /users/me/plan
# ---------------------------------------------------------------------------

def get_plan_status(user: User, current_plant_count: int, current_grow_count: int) -> dict:
    """Retorna um resumo do plano atual com limites e consumo."""
    effective = _effective_plan(user)
    limits = _plan_limits(effective)

    is_expired = (
        user.plan in ("grower", "pro")
        and user.plan_expires_at is not None
        and user.plan_expires_at < datetime.now(timezone.utc)
    )

    ai_limit = limits["ai_queries_per_month"]
    ai_used = user.ai_queries_this_month

    return {
        "plan": user.plan,
        "effective_plan": effective,
        "is_expired": is_expired,
        "plan_expires_at": user.plan_expires_at,
        # Limites
        "max_plants": limits["max_plants"],
        "max_grows": limits["max_grows"],
        "max_pots_per_grow": limits["max_pots_per_grow"],
        "ai_queries_per_month": ai_limit if ai_limit < 999999 else None,
        # Uso atual
        "plants_used": current_plant_count,
        "grows_used": current_grow_count,
        "ai_queries_used": ai_used,
        "ai_queries_reset_at": user.ai_queries_reset_at,
    }
