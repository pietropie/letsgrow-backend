"""
Creditos do usuario.

GET  /credits/balance               – saldo (plano + extras) e renovacao
GET  /credits/ledger                – historico (tarefas, concessoes, compras, expiracoes, ajustes)
GET  /credits/packages              – pacotes avulsos ativos (cadastrados no admin)
POST /credits/quote                 – custo antes da tarefa ("Antes de comecar")
POST /credits/purchases             – inicia compra (503 enquanto nao houver provedor)
GET  /credits/purchases/{id}        – estado da compra
POST /credits/webhooks/{provider}   – confirmacao do provedor de pagamento
"""
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import get_db
from app.models.credits import CREDIT_TASKS, CreditLedgerEntry, CreditPackage, CreditPurchase, CreditQuote
from app.models.grow import Grow
from app.models.plant import Plant
from app.models.user import User
from app.services.app_settings import get_app_settings
from app.services.auth import get_current_user
from app.services.credits import CreditError, create_quote, grant_lot, refresh_balance
from app.services.payments import InvalidWebhook, PaymentUnavailable, get_payment_provider

logger = logging.getLogger(__name__)

router = APIRouter()


class BalanceOut(BaseModel):
    enabled: bool
    available: int
    plan_available: int
    extra_available: int
    renews_at: datetime | None
    monthly_credits: int | None
    replaces_query_limit: bool


class HistoryItem(BaseModel):
    id: str
    # task | grant_plan | purchase | expire | adjust
    type: str
    title: str
    amount: int                       # efeito liquido no saldo
    status: str | None = None         # tarefas: reserved | captured | released
    cost: int | None = None
    task_key: str | None = None
    task_ref: str | None = None       # ex.: id da conversa para reler a resposta sem custo
    failure_reason: str | None = None
    occurred_at: datetime


class HistoryPage(BaseModel):
    items: list[HistoryItem]
    total: int


class PackageOut(BaseModel):
    id: uuid.UUID
    label: str
    credits: int
    price_brl: float
    validity_days: int | None


class QuoteIn(BaseModel):
    task: Literal["chat_message", "photo_analysis"]
    grow_id: uuid.UUID | None = None
    plant_id: uuid.UUID | None = None

    model_config = {"extra": "forbid"}


class QuoteOut(BaseModel):
    required: bool                # false = a tarefa nao cobra creditos agora
    quote_id: uuid.UUID | None
    task: str
    task_label: str
    cost: int
    available: int
    after: int
    sufficient: bool
    scope: dict | None
    expires_at: datetime | None


class PurchaseIn(BaseModel):
    package_id: uuid.UUID


class PurchaseOut(BaseModel):
    id: uuid.UUID
    status: str
    label: str
    credits: int
    price_brl: float
    checkout_url: str | None
    paid_at: datetime | None


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


@router.get("/balance", response_model=BalanceOut)
async def get_balance(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    balance = await refresh_balance(db, current_user)
    app_settings = await get_app_settings(db)
    return BalanceOut(
        **balance.__dict__,
        replaces_query_limit=app_settings.credits_enabled and app_settings.credits_replace_query_limit,
    )


@router.get("/ledger", response_model=HistoryPage)
async def get_history(
    limit: int = Query(default=30, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    items: list[HistoryItem] = []
    quotes = (
        await db.execute(
            select(CreditQuote).where(
                CreditQuote.user_id == current_user.id,
                CreditQuote.status.in_(("reserved", "captured", "released")),
            )
        )
    ).scalars().all()
    for quote in quotes:
        items.append(HistoryItem(
            id=f"task:{quote.id}", type="task", title=CREDIT_TASKS.get(quote.task_key, quote.task_key),
            amount=0 if quote.status == "released" else -quote.cost, status=quote.status, cost=quote.cost,
            task_key=quote.task_key, task_ref=quote.task_ref, failure_reason=quote.failure_reason,
            occurred_at=_utc(quote.created_at),
        ))

    entries = (
        await db.execute(
            select(CreditLedgerEntry).where(
                CreditLedgerEntry.user_id == current_user.id,
                CreditLedgerEntry.kind.in_(("grant_plan", "purchase", "expire", "adjust")),
            )
        )
    ).scalars().all()
    # Expiracoes/ajustes podem ser divididos entre lotes: agrupa pelo mesmo instante.
    grouped: dict[tuple, HistoryItem] = {}
    for entry in entries:
        key = (entry.kind, entry.description, _utc(entry.created_at)) if entry.kind in ("expire", "adjust") else (entry.id,)
        if key in grouped:
            grouped[key].amount += entry.amount
            continue
        grouped[key] = HistoryItem(
            id=f"{entry.kind}:{entry.id}", type=entry.kind, title=entry.description,
            amount=entry.amount, occurred_at=_utc(entry.created_at),
        )
    items.extend(grouped.values())
    items.sort(key=lambda item: item.occurred_at, reverse=True)
    return HistoryPage(items=items[offset:offset + limit], total=len(items))


@router.get("/packages", response_model=list[PackageOut])
async def list_packages(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    app_settings = await get_app_settings(db)
    if not app_settings.credits_enabled:
        return []
    rows = (
        await db.execute(
            select(CreditPackage)
            .where(CreditPackage.is_active == True)  # noqa: E712
            .order_by(CreditPackage.sort_order.asc(), CreditPackage.credits.asc())
        )
    ).scalars().all()
    return [
        PackageOut(id=p.id, label=p.label, credits=p.credits, price_brl=float(p.price_brl), validity_days=p.validity_days)
        for p in rows
    ]


@router.post("/quote", response_model=QuoteOut)
async def quote_task(
    body: QuoteIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    scope: dict = {}
    if body.plant_id is not None:
        plant = (
            await db.execute(select(Plant).where(Plant.id == body.plant_id, Plant.user_id == current_user.id))
        ).scalar_one_or_none()
        if plant is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Planta não encontrada")
        scope.update(plant_id=str(plant.id), plant_name=plant.display_name or plant.strain_name)
    if body.grow_id is not None:
        grow = (
            await db.execute(select(Grow).where(Grow.id == body.grow_id, Grow.user_id == current_user.id))
        ).scalar_one_or_none()
        if grow is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Ambiente não encontrado")
        scope.update(grow_id=str(grow.id), grow_name=grow.name)

    quote, balance = await create_quote(db, current_user, body.task, scope or None)
    cost = quote.cost if quote else 0
    return QuoteOut(
        required=quote is not None,
        quote_id=quote.id if quote else None,
        task=body.task,
        task_label=CREDIT_TASKS[body.task],
        cost=cost,
        available=balance.available,
        after=balance.available - cost,
        sufficient=balance.available >= cost,
        scope=scope or None,
        expires_at=quote.expires_at if quote else None,
    )


def _purchase_out(purchase: CreditPurchase) -> PurchaseOut:
    return PurchaseOut(
        id=purchase.id, status=purchase.status, label=purchase.label, credits=purchase.credits,
        price_brl=float(purchase.price_brl), checkout_url=purchase.checkout_url, paid_at=purchase.paid_at,
    )


@router.post("/purchases", response_model=PurchaseOut, status_code=status.HTTP_201_CREATED)
async def start_purchase(
    body: PurchaseIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    app_settings = await get_app_settings(db)
    if not app_settings.credits_enabled:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Créditos não estão ativos")
    package = await db.get(CreditPackage, body.package_id)
    if package is None or not package.is_active:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Pacote não encontrado")

    provider = get_payment_provider()
    now = datetime.now(timezone.utc)
    purchase = CreditPurchase(
        id=uuid.uuid4(), user_id=current_user.id, package_id=package.id, label=package.label,
        credits=package.credits, price_brl=package.price_brl, validity_days=package.validity_days,
        provider=provider.name, status="pending", created_at=now, updated_at=now,
    )
    try:
        session = await provider.create_checkout(purchase, current_user)
    except PaymentUnavailable as exc:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc))
    purchase.provider_ref = session.provider_ref
    purchase.checkout_url = session.checkout_url
    db.add(purchase)
    await db.commit()
    return _purchase_out(purchase)


@router.get("/purchases/{purchase_id}", response_model=PurchaseOut)
async def get_purchase(
    purchase_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    purchase = (
        await db.execute(
            select(CreditPurchase).where(CreditPurchase.id == purchase_id, CreditPurchase.user_id == current_user.id)
        )
    ).scalar_one_or_none()
    if purchase is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Compra não encontrada")
    return _purchase_out(purchase)


async def apply_purchase_status(db: AsyncSession, purchase: CreditPurchase, new_status: str) -> None:
    """Idempotente: so credita na primeira transicao pending -> paid."""
    if purchase.status != "pending" or new_status not in ("paid", "failed", "canceled"):
        return
    now = datetime.now(timezone.utc)
    purchase.status = new_status
    purchase.updated_at = now
    if new_status == "paid":
        purchase.paid_at = now
        if purchase.user_id is not None:
            await grant_lot(
                db, purchase.user_id, kind="purchase", amount=purchase.credits,
                description=f"{purchase.credits} créditos adicionais · {purchase.label}",
                expires_at=now + timedelta(days=purchase.validity_days) if purchase.validity_days else None,
                purchase_id=purchase.id, now=now,
            )
    await db.commit()


@router.post("/webhooks/{provider_name}", status_code=status.HTTP_204_NO_CONTENT)
async def payment_webhook(
    provider_name: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    provider = get_payment_provider()
    if provider_name != provider.name or provider.name == "unconfigured":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Provedor não configurado")
    try:
        event = await provider.parse_webhook(await request.body(), dict(request.headers))
    except InvalidWebhook:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Webhook inválido")
    purchase = (
        await db.execute(
            select(CreditPurchase).where(
                CreditPurchase.provider == provider.name, CreditPurchase.provider_ref == event.provider_ref
            )
        )
    ).scalar_one_or_none()
    if purchase is None:
        logger.warning("Webhook %s sem compra correspondente: %s", provider.name, event.provider_ref)
        return
    await apply_purchase_status(db, purchase, event.status)


def credit_http_error(exc: CreditError) -> HTTPException:
    code = status.HTTP_402_PAYMENT_REQUIRED if exc.code == "insufficient_credits" else status.HTTP_409_CONFLICT
    if exc.code == "quote_not_found":
        code = status.HTTP_404_NOT_FOUND
    return HTTPException(status_code=code, detail=exc.detail())


async def start_paid_task(
    db: AsyncSession, user: User, task_key: str, quote_id: uuid.UUID | None,
) -> CreditQuote | None:
    """Aplica o limite mensal de consultas e reserva os creditos da tarefa, se paga.

    Retorna a reserva (ou None quando a tarefa nao cobra creditos). Sem cotacao
    confirmada para uma tarefa paga, responde 402 com code=quote_required: o app
    mostra o custo ("Antes de comecar") e repete o pedido com quote_id. Com
    credits_replace_query_limit ligado, tarefas pagas nao contam no limite mensal.
    """
    from app.services.credits import reserve, task_cost
    from app.services.subscription import check_ai_limit

    app_settings = await get_app_settings(db)
    cost = await task_cost(db, task_key, app_settings)
    if cost is None or not app_settings.credits_replace_query_limit:
        check_ai_limit(user)
    if cost is None:
        return None
    if quote_id is None:
        raise HTTPException(
            status_code=status.HTTP_402_PAYMENT_REQUIRED,
            detail={"code": "quote_required", "message": "Confirme o custo antes de continuar.",
                    "task": task_key, "cost": cost},
        )
    try:
        return await reserve(db, user, quote_id, task_key)
    except CreditError as exc:
        raise credit_http_error(exc)
