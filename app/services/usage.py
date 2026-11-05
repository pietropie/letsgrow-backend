"""
Uso do mês num lugar só, para o mostrador do Início e da tela de plano (GET /usage).

Modos:
- queries: o plano tem limite de consultas e os créditos não o substituem;
- credits: créditos ligados e alguma tarefa cobra créditos (sem saldo, aparece esgotado);
- both: os dois valem ao mesmo tempo;
- unlimited: nenhum dos dois limita.

O "usado" em créditos vem do extrato do período (reservas menos estornos), e não de
monthly_credits − plan_available: com sobra de mês anterior, ajuste do admin ou
plano trocado no meio do mês essa conta não fecha.
"""
from __future__ import annotations

from datetime import datetime, time, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.credits import CreditLedgerEntry, CreditTaskCost
from app.models.user import User
from app.services import credits as credit_service
from app.services.app_settings import get_app_settings
from app.services.plan_config_service import get_all_plan_configs
from app.services.subscription import _effective_plan, _plan_limits, next_ai_quota_reset


def _month_start_utc(now: datetime) -> datetime:
    return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def _queries_used(user: User, now: datetime) -> int:
    """O worker zera a cota no começo do mês; até ele rodar, o contador ainda é do mês passado."""
    reset_at = credit_service._utc(user.ai_queries_reset_at)
    if reset_at is None or reset_at < _month_start_utc(now):
        return 0
    return user.ai_queries_this_month or 0


async def _consumed(db: AsyncSession, user: User, since: datetime, bucket: str | None = None) -> int:
    """Créditos consumidos desde `since`: reservas (negativas) menos os estornos."""
    query = select(func.coalesce(func.sum(CreditLedgerEntry.amount), 0)).where(
        CreditLedgerEntry.user_id == user.id,
        CreditLedgerEntry.kind.in_(("reserve", "release")),
        CreditLedgerEntry.created_at >= since,
    )
    if bucket is not None:
        query = query.where(CreditLedgerEntry.bucket == bucket)
    return max(0, -int((await db.execute(query)).scalar_one()))


async def _plan_granted(db: AsyncSession, user: User, period_start) -> int | None:
    return (
        await db.execute(
            select(CreditLedgerEntry.amount).where(
                CreditLedgerEntry.user_id == user.id,
                CreditLedgerEntry.kind == "grant_plan",
                CreditLedgerEntry.period_start == period_start,
            )
        )
    ).scalar_one_or_none()


async def _next_expiry(db: AsyncSession, user: User, now: datetime) -> dict | None:
    """O próximo vencimento de créditos ainda não usados: quando e quantos."""
    expiring: dict[datetime, int] = {}
    for lot, remaining in await credit_service._lots(db, user.id):
        expires = credit_service._utc(lot.expires_at)
        if remaining > 0 and expires is not None and expires > now:
            expiring[expires] = expiring.get(expires, 0) + remaining
    if not expiring:
        return None
    at = min(expiring)
    return {"at": at, "credits": expiring[at]}


def _ratio(used: int, total: int) -> float:
    return used / total if total > 0 else 1.0


async def get_usage(db: AsyncSession, user: User, now: datetime | None = None) -> dict:
    now = now or datetime.now(timezone.utc)
    app_settings = await get_app_settings(db)
    threshold = app_settings.usage_warning_threshold
    await get_all_plan_configs(db)  # aquece o cache que _plan_limits lê
    query_limit = _plan_limits(_effective_plan(user))["ai_queries_per_month"]
    # Créditos só limitam alguma coisa se estão ligados e alguma tarefa cobra
    credits_on = app_settings.credits_enabled and (
        await db.execute(
            select(CreditTaskCost.task_key).where(CreditTaskCost.is_active.is_(True), CreditTaskCost.credits > 0).limit(1)
        )
    ).scalar_one_or_none() is not None
    queries_limited = query_limit < 999999 and not (credits_on and app_settings.credits_replace_query_limit)

    queries = None
    ratios: list[float] = []
    exhausted: list[bool] = []
    if queries_limited:
        used = min(_queries_used(user, now), query_limit)
        remaining = query_limit - used
        queries = {"used": used, "total": query_limit, "remaining": remaining}
        ratios.append(_ratio(used, query_limit))
        exhausted.append(remaining <= 0)

    credits = None
    credit_period = None
    if credits_on:
        balance = await credit_service.refresh_balance(db, user, now)
        start_date, renews = credit_service.period_bounds(now)
        start = datetime.combine(start_date, time(), tzinfo=credit_service._BRAZIL).astimezone(timezone.utc)
        used_in_period = await _consumed(db, user, start)
        granted = await _plan_granted(db, user, start_date)
        credits = {
            "plan_total": granted if granted is not None else (balance.monthly_credits or 0),
            "plan_used": await _consumed(db, user, start, bucket="plan"),
            "extra_balance": balance.extra_available,
            "used_in_period": used_in_period,
            "remaining": balance.available,
            "next_expiry": await _next_expiry(db, user, now),
        }
        credit_period = {"start": start, "renews_at": renews}
        # Fração do que havia no mês (usado + o que resta) que já foi usada; sem nada, está esgotado
        ratios.append(_ratio(used_in_period, used_in_period + balance.available))
        exhausted.append(balance.available <= 0)

    if queries and credits:
        mode = "both"
    elif credits:
        mode = "credits"
    elif queries:
        mode = "queries"
    else:
        mode = "unlimited"

    if credit_period is not None:
        period = credit_period
    else:
        reset_at = credit_service._utc(user.ai_queries_reset_at) or now
        period = {"start": _month_start_utc(now), "renews_at": next_ai_quota_reset(reset_at, now)}

    is_exhausted = any(exhausted)
    return {
        "mode": mode,
        "period": period,
        "queries": queries,
        "credits": credits,
        "near_limit": is_exhausted or any(ratio >= threshold for ratio in ratios),
        "exhausted": is_exhausted,
        "warning_threshold": threshold,
    }
