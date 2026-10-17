"""
Creditos: saldo por lotes, cotacao, reserva, captura, estorno e expiracao.

Nada aqui define preco ou custo: valores vem de plan_configs.monthly_credits,
credit_task_costs e credit_packages (admin). Com app_settings.credits_enabled
desligado, nenhuma tarefa e cobrada e nenhum credito de plano e concedido.

Lote = lancamento positivo sem lot_id (grant_plan, purchase, adjust positivo).
Lancamentos que consomem/devolvem apontam para o lote (reserve, release,
expire, adjust negativo). Saldo de um lote = amount + soma dos filhos.
"""
from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.app_settings import AppSettings
from app.models.credits import CREDIT_TASKS, CreditLedgerEntry, CreditQuote, CreditTaskCost
from app.models.user import User

logger = logging.getLogger(__name__)

_BRAZIL = ZoneInfo("America/Sao_Paulo")
QUOTE_TTL = timedelta(minutes=15)
# Reserva sem captura/estorno por mais tempo que isso e devolvida pela manutencao.
STALE_RESERVATION = timedelta(hours=1)
_MONTHS = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]


class CreditError(Exception):
    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code = code
        self.message = message
        self.extra = extra

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


@dataclass
class Balance:
    enabled: bool
    available: int
    plan_available: int
    extra_available: int
    renews_at: datetime | None
    monthly_credits: int | None


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def period_bounds(now: datetime) -> tuple[date, datetime]:
    """Mes civil no horario de Brasilia: (primeiro dia, inicio do proximo mes em UTC)."""
    local = now.astimezone(_BRAZIL)
    start = local.date().replace(day=1)
    next_month = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    renews = datetime(next_month.year, next_month.month, 1, tzinfo=_BRAZIL).astimezone(timezone.utc)
    return start, renews


async def plan_monthly_credits(db: AsyncSession, user: User) -> int | None:
    from app.services.plan_config_service import get_plan_config
    from app.services.subscription import _effective_plan

    config = await get_plan_config(db, _effective_plan(user))
    return config.get("monthly_credits") if config else None


async def task_cost(db: AsyncSession, task_key: str, app_settings: AppSettings) -> int | None:
    """Custo ativo da tarefa, ou None se ela nao cobra creditos."""
    if not app_settings.credits_enabled or task_key not in CREDIT_TASKS:
        return None
    row = await db.get(CreditTaskCost, task_key)
    if row is None or not row.is_active or row.credits <= 0:
        return None
    return row.credits


async def ensure_plan_grant(db: AsyncSession, user: User, app_settings: AppSettings, now: datetime) -> None:
    if not app_settings.credits_enabled:
        return
    monthly = await plan_monthly_credits(db, user)
    if not monthly or monthly <= 0:
        return
    start, renews = period_bounds(now)
    exists = (
        await db.execute(
            select(CreditLedgerEntry.id).where(
                CreditLedgerEntry.user_id == user.id, CreditLedgerEntry.period_start == start
            )
        )
    ).scalar_one_or_none()
    if exists is not None:
        return
    db.add(CreditLedgerEntry(
        id=uuid.uuid4(), user_id=user.id, kind="grant_plan", bucket="plan", amount=monthly,
        expires_at=None if app_settings.plan_credits_rollover else renews,
        period_start=start, created_at=now,
        description=f"Créditos do plano · {_MONTHS[start.month - 1]} {start.year}",
    ))
    try:
        await db.commit()
    except IntegrityError:
        # Outra requisicao concedeu o mesmo periodo ao mesmo tempo.
        await db.rollback()


async def _lots(db: AsyncSession, user_id: uuid.UUID) -> list[tuple[CreditLedgerEntry, int]]:
    """Lotes do usuario com o saldo restante de cada um."""
    lots = (
        await db.execute(
            select(CreditLedgerEntry).where(
                CreditLedgerEntry.user_id == user_id,
                CreditLedgerEntry.lot_id.is_(None),
                CreditLedgerEntry.amount > 0,
            )
        )
    ).scalars().all()
    if not lots:
        return []
    sums = dict(
        (
            await db.execute(
                select(CreditLedgerEntry.lot_id, func.sum(CreditLedgerEntry.amount))
                .where(CreditLedgerEntry.user_id == user_id, CreditLedgerEntry.lot_id.is_not(None))
                .group_by(CreditLedgerEntry.lot_id)
            )
        ).all()
    )
    return [(lot, lot.amount + int(sums.get(lot.id) or 0)) for lot in lots]


def _lot_order(item: tuple[CreditLedgerEntry, int]):
    lot, _ = item
    expires = _utc(lot.expires_at)
    # Plano primeiro; depois o que vence antes; sem validade por ultimo.
    return (lot.bucket != "plan", expires is None, expires or datetime.max.replace(tzinfo=timezone.utc), lot.created_at)


async def expire_lots(db: AsyncSession, user_id: uuid.UUID, now: datetime) -> None:
    changed = False
    for lot, remaining in await _lots(db, user_id):
        expires = _utc(lot.expires_at)
        if expires is not None and expires <= now and remaining > 0:
            db.add(CreditLedgerEntry(
                id=uuid.uuid4(), user_id=user_id, kind="expire", bucket=lot.bucket, amount=-remaining,
                lot_id=lot.id, created_at=now, description="Créditos expirados",
            ))
            changed = True
    if changed:
        await db.commit()


async def refresh_balance(db: AsyncSession, user: User, now: datetime | None = None) -> Balance:
    from app.services.app_settings import get_app_settings

    now = now or datetime.now(timezone.utc)
    app_settings = await get_app_settings(db)
    await ensure_plan_grant(db, user, app_settings, now)
    await expire_lots(db, user.id, now)
    plan = extra = 0
    for lot, remaining in await _lots(db, user.id):
        expires = _utc(lot.expires_at)
        if remaining <= 0 or (expires is not None and expires <= now):
            continue
        if lot.bucket == "plan":
            plan += remaining
        else:
            extra += remaining
    monthly = await plan_monthly_credits(db, user)
    _, renews = period_bounds(now)
    return Balance(
        enabled=app_settings.credits_enabled,
        available=plan + extra,
        plan_available=plan,
        extra_available=extra,
        renews_at=renews if app_settings.credits_enabled and monthly else None,
        monthly_credits=monthly,
    )


async def create_quote(
    db: AsyncSession, user: User, task_key: str, scope: dict | None, now: datetime | None = None,
) -> tuple[CreditQuote | None, Balance]:
    """Cotacao para a tela "Antes de comecar". None = a tarefa nao cobra creditos."""
    from app.services.app_settings import get_app_settings

    if task_key not in CREDIT_TASKS:
        raise CreditError("unknown_task", "Tarefa desconhecida")
    now = now or datetime.now(timezone.utc)
    balance = await refresh_balance(db, user, now)
    cost = await task_cost(db, task_key, await get_app_settings(db))
    if cost is None:
        return None, balance
    quote = CreditQuote(
        id=uuid.uuid4(), user_id=user.id, task_key=task_key, cost=cost, scope=scope,
        status="quoted", created_at=now, expires_at=now + QUOTE_TTL,
    )
    db.add(quote)
    await db.commit()
    return quote, balance


async def reserve(
    db: AsyncSession, user: User, quote_id: uuid.UUID, task_key: str, now: datetime | None = None,
) -> CreditQuote:
    """Transforma a cotacao confirmada em reserva. Cada cotacao so pode ser usada uma vez."""
    now = now or datetime.now(timezone.utc)
    # Concessao do mes e expiracoes antes do bloqueio (podem fazer commit).
    await refresh_balance(db, user, now)
    # Serializa debitos do mesmo usuario ate o commit (no-op no SQLite dos testes).
    await db.execute(select(User.id).where(User.id == user.id).with_for_update())
    quote = (
        await db.execute(
            select(CreditQuote).where(CreditQuote.id == quote_id, CreditQuote.user_id == user.id)
        )
    ).scalar_one_or_none()
    if quote is None or quote.task_key != task_key:
        raise CreditError("quote_not_found", "Cotação não encontrada")
    if quote.status != "quoted":
        raise CreditError("quote_used", "Esta cotação já foi usada")
    if _utc(quote.expires_at) <= now:
        quote.status = "expired"
        await db.commit()
        raise CreditError("quote_expired", "A cotação expirou. Confira o custo novamente.")

    usable = [
        (lot, remaining) for lot, remaining in await _lots(db, user.id)
        if remaining > 0 and (_utc(lot.expires_at) is None or _utc(lot.expires_at) > now)
    ]
    available = sum(remaining for _, remaining in usable)
    if available < quote.cost:
        raise CreditError(
            "insufficient_credits", "Saldo insuficiente. A tarefa não foi iniciada.",
            cost=quote.cost, available=available,
        )
    needed = quote.cost
    for lot, remaining in sorted(usable, key=_lot_order):
        if needed <= 0:
            break
        take = min(needed, remaining)
        db.add(CreditLedgerEntry(
            id=uuid.uuid4(), user_id=user.id, kind="reserve", bucket=lot.bucket, amount=-take,
            lot_id=lot.id, quote_id=quote.id, task_key=task_key, created_at=now,
            description=f"Reserva · {CREDIT_TASKS[task_key]}",
        ))
        needed -= take
    quote.status = "reserved"
    await db.commit()
    return quote


async def capture(db: AsyncSession, quote: CreditQuote, task_ref: str | None, now: datetime | None = None) -> None:
    """Tarefa entregue: a reserva vira consumo (lancamento informativo de valor 0).

    Nao faz commit: o chamador grava junto com o resultado da tarefa.
    """
    now = now or datetime.now(timezone.utc)
    if quote.status != "reserved":
        return
    quote.status = "captured"
    quote.task_ref = task_ref
    quote.settled_at = now
    db.add(CreditLedgerEntry(
        id=uuid.uuid4(), user_id=quote.user_id, kind="capture", bucket="plan", amount=0,
        quote_id=quote.id, task_key=quote.task_key, task_ref=task_ref, created_at=now,
        description=f"{quote.cost} créditos utilizados · {CREDIT_TASKS.get(quote.task_key, quote.task_key)}",
    ))


async def release(db: AsyncSession, quote: CreditQuote, reason: str, now: datetime | None = None) -> None:
    """Falha tecnica sem entrega: devolve cada parte da reserva ao lote de origem."""
    now = now or datetime.now(timezone.utc)
    if quote.status != "reserved":
        return
    reserved = (
        await db.execute(
            select(CreditLedgerEntry).where(
                CreditLedgerEntry.quote_id == quote.id, CreditLedgerEntry.kind == "reserve"
            )
        )
    ).scalars().all()
    for entry in reserved:
        db.add(CreditLedgerEntry(
            id=uuid.uuid4(), user_id=quote.user_id, kind="release", bucket=entry.bucket,
            amount=-entry.amount, lot_id=entry.lot_id, quote_id=quote.id, task_key=quote.task_key,
            created_at=now, description=f"Reserva de {quote.cost} créditos liberada",
        ))
    quote.status = "released"
    quote.failure_reason = reason[:200]
    quote.settled_at = now
    await db.commit()


async def grant_lot(
    db: AsyncSession, user_id: uuid.UUID, *, kind: str, amount: int, description: str,
    expires_at: datetime | None = None, purchase_id: uuid.UUID | None = None, now: datetime | None = None,
) -> CreditLedgerEntry:
    entry = CreditLedgerEntry(
        id=uuid.uuid4(), user_id=user_id, kind=kind, bucket="extra", amount=amount,
        expires_at=expires_at, purchase_id=purchase_id,
        created_at=now or datetime.now(timezone.utc), description=description[:200],
    )
    db.add(entry)
    return entry


async def debit_adjustment(
    db: AsyncSession, user: User, amount: int, description: str, now: datetime | None = None,
) -> None:
    """Ajuste negativo do admin: retira dos lotes na mesma ordem de consumo."""
    now = now or datetime.now(timezone.utc)
    balance = await refresh_balance(db, user, now)
    if balance.available < amount:
        raise CreditError("insufficient_credits", "Saldo insuficiente para o ajuste", available=balance.available)
    needed = amount
    usable = [(lot, rem) for lot, rem in await _lots(db, user.id) if rem > 0]
    for lot, remaining in sorted(usable, key=_lot_order):
        if needed <= 0:
            break
        take = min(needed, remaining)
        db.add(CreditLedgerEntry(
            id=uuid.uuid4(), user_id=user.id, kind="adjust", bucket=lot.bucket, amount=-take,
            lot_id=lot.id, created_at=now, description=description[:200],
        ))
        needed -= take


async def release_stale_reservations(db: AsyncSession, now: datetime | None = None) -> int:
    now = now or datetime.now(timezone.utc)
    stale = (
        await db.execute(
            select(CreditQuote).where(
                CreditQuote.status == "reserved", CreditQuote.created_at < now - STALE_RESERVATION
            )
        )
    ).scalars().all()
    for quote in stale:
        await release(db, quote, "Sem entrega", now)
    return len(stale)
