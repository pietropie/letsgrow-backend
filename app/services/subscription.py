"""
Serviço de controle de limites de plano.

Todos os limites vêm de config.py (variáveis de ambiente) — sem tabela no
banco, sem redeploy para alterar. Para limites dinâmicos em runtime, migrar
para uma tabela `plans` no futuro.

Planos: free | jardineiro | cultivador | grower_pro
Legado (mantido por compatibilidade): grower → cultivador, pro → grower_pro
Expiração: se plan_expires_at < agora, o plano pago é tratado como free.
"""
from datetime import datetime, timezone

from fastapi import HTTPException, status

from app.config import settings
from app.models.user import User

# Planos pagos (qualquer um desses reverte para free ao expirar)
PAID_PLANS = ("jardineiro", "cultivador", "grower_pro", "grower", "pro")


# ---------------------------------------------------------------------------
# Helpers internos
# ---------------------------------------------------------------------------

def _plan_limits(plan: str) -> dict:
    # grower_pro (e legado "pro")
    if plan in ("grower_pro", "pro"):
        return {
            "max_grows": settings.PRO_MAX_GROWS,
            "max_pots_per_grow": settings.PRO_MAX_POTS_PER_GROW,
            "max_plants": settings.PRO_MAX_PLANTS,
            "ai_queries_per_month": 999999,
            "sensors_allowed": True,
        }
    # cultivador (e legado "grower")
    if plan in ("cultivador", "grower"):
        return {
            "max_grows": settings.CULTIVADOR_MAX_GROWS,
            "max_pots_per_grow": settings.CULTIVADOR_MAX_POTS_PER_GROW,
            "max_plants": settings.CULTIVADOR_MAX_PLANTS,
            "ai_queries_per_month": 999999,
            "sensors_allowed": True,
        }
    # jardineiro — cultivador caseiro (R$24,90/mês)
    if plan == "jardineiro":
        return {
            "max_grows": settings.JARDINEIRO_MAX_GROWS,
            "max_pots_per_grow": settings.JARDINEIRO_MAX_POTS_PER_GROW,
            "max_plants": settings.JARDINEIRO_MAX_PLANTS,
            "ai_queries_per_month": settings.JARDINEIRO_AI_QUERIES_PER_MONTH,
            "sensors_allowed": False,
        }
    # free (default)
    return {
        "max_grows": settings.FREE_MAX_GROWS,
        "max_pots_per_grow": settings.FREE_MAX_POTS_PER_GROW,
        "max_plants": settings.FREE_MAX_PLANTS,
        "ai_queries_per_month": settings.FREE_AI_QUERIES_PER_MONTH,
        "sensors_allowed": False,
    }


def _effective_plan(user: User) -> str:
    """Retorna o plano real do usuário, revertendo para 'free' se expirado."""
    if user.plan in PAID_PLANS:
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
    ai_limit = limits["ai_queries_per_month"]
    if ai_limit < 999999 and user.ai_queries_this_month >= ai_limit:
        plan_display = {"free": "Free", "jardineiro": "Jardineiro"}.get(plan, plan.upper())
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Limite de {ai_limit} consultas de IA atingido este mês. "
                f"Faça upgrade para continuar usando o Bob."
            ),
        )


def check_sensor_access(user: User) -> None:
    """Verifica se o plano permite conectar sensores (Cultivador ou superior)."""
    plan = _effective_plan(user)
    limits = _plan_limits(plan)
    if not limits["sensors_allowed"]:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Sensores disponíveis a partir do plano Cultivador. Faça upgrade para ativar.",
        )


# ---------------------------------------------------------------------------
# Status do plano — usado pelo endpoint GET /users/me/plan
# ---------------------------------------------------------------------------

def get_plan_status(user: User, current_plant_count: int, current_grow_count: int) -> dict:
    """Retorna um resumo do plano atual com limites e consumo."""
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
        # Limites
        "max_plants": limits["max_plants"],
        "max_grows": limits["max_grows"],
        "max_pots_per_grow": limits["max_pots_per_grow"],
        "ai_queries_per_month": ai_limit if ai_limit < 999999 else None,
        "sensors_allowed": limits["sensors_allowed"],
        # Uso atual
        "plants_used": current_plant_count,
        "grows_used": current_grow_count,
        "ai_queries_used": user.ai_queries_this_month,
        "ai_queries_reset_at": user.ai_queries_reset_at,
    }
