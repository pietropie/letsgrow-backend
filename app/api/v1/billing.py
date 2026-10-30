"""
Assinaturas e pagamentos (contrato com o webapp e o app Android).

GET  /billing/catalog                – planos a venda, precos por periodo e IDs do Google Play
GET  /billing/subscription           – plano da conta, assinatura atual e quem a gerencia
GET  /billing/subscriptions/{id}     – estado de uma assinatura (volta do checkout na web)
POST /billing/checkout               – web: assinatura (cartao/Pix) ou creditos -> checkout_url do Mercado Pago
POST /billing/subscription/cancel    – web: desliga a renovacao (o periodo pago continua)
POST /billing/google/verify          – Android: valida a compra no Google Play e libera
POST /billing/webhooks/mercadopago   – notificacoes do Mercado Pago (x-signature)
POST /billing/webhooks/google-play   – Real-time Developer Notifications (push do Pub/Sub)
"""
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1 import credits
from app.database import get_db
from app.models.billing import PLAN_PERIODS, Subscription
from app.models.user import User
from app.services import billing
from app.services import google_play as gp
from app.services import mercadopago as mp
from app.services.auth import get_current_user
from app.services.subscription import _effective_plan

logger = logging.getLogger(__name__)

router = APIRouter()


class PeriodPrice(BaseModel):
    period: str
    months: int
    price_brl: float | None
    # Android: assinatura e base plan no Play Console (o preco exibido vem do Play)
    google_product_id: str | None
    google_base_plan_id: str | None


class CatalogPlan(BaseModel):
    plan_key: str
    label: str
    periods: list[PeriodPrice]


class CatalogOut(BaseModel):
    web_enabled: bool
    android_enabled: bool
    plans: list[CatalogPlan]


class SubscriptionOut(BaseModel):
    id: uuid.UUID
    plan_key: str
    period: str
    provider: str
    kind: str
    status: str
    auto_renew: bool
    price_brl: float | None
    current_period_start: datetime | None
    current_period_end: datetime | None
    access_until: datetime | None
    checkout_url: str | None
    managed_by: str | None


class AccountBillingOut(BaseModel):
    plan: str
    effective_plan: str
    plan_expires_at: datetime | None
    current: SubscriptionOut | None
    # Assinatura que comeca depois (downgrade, troca de periodo, renovacao por Pix)
    upcoming: SubscriptionOut | None
    # web | google_play | null: onde o usuario gerencia/cancela
    managed_by: str | None
    play_manage_url: str


class CheckoutIn(BaseModel):
    kind: Literal["subscription", "credits"]
    plan_key: str | None = None
    period: Literal["monthly", "yearly"] | None = None
    package_id: uuid.UUID | None = None
    method: Literal["card", "pix"] = "card"

    model_config = {"extra": "forbid"}


class CheckoutOut(BaseModel):
    kind: str
    id: uuid.UUID
    status: str
    checkout_url: str | None


class GoogleVerifyIn(BaseModel):
    kind: Literal["subscription", "credits"]
    product_id: str = Field(min_length=1, max_length=100)
    purchase_token: str = Field(min_length=1, max_length=500)

    model_config = {"extra": "forbid"}


class GoogleVerifyOut(BaseModel):
    kind: str
    subscription: SubscriptionOut | None = None
    purchase: credits.PurchaseOut | None = None


def _http(exc: billing.BillingError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.detail())


def _sub_out(sub: Subscription) -> SubscriptionOut:
    return SubscriptionOut(
        id=sub.id, plan_key=sub.plan_key, period=sub.period, provider=sub.provider, kind=sub.kind,
        status=sub.status, auto_renew=sub.auto_renew,
        price_brl=float(sub.price_brl) if sub.price_brl is not None else None,
        current_period_start=sub.current_period_start, current_period_end=sub.current_period_end,
        access_until=billing.access_until(sub),
        checkout_url=sub.checkout_url if sub.status == "pending" else None,
        managed_by=billing.managed_by(sub),
    )


@router.get("/catalog", response_model=CatalogOut)
async def get_catalog(db: AsyncSession = Depends(get_db)):
    labels = await billing.plan_labels(db)
    prices = {(p.plan_key, p.period): p for p in await billing.plan_prices(db)}
    plans = []
    for plan_key in ("jardineiro", "cultivador", "grower_pro"):
        periods = [
            PeriodPrice(period=period, months=months, price_brl=float(price.price_brl),
                        google_product_id=price.google_product_id, google_base_plan_id=price.google_base_plan_id)
            for period, months in PLAN_PERIODS.items()
            if billing.is_sellable(price := prices.get((plan_key, period)))
        ]
        if periods:
            plans.append(CatalogPlan(plan_key=plan_key, label=labels.get(plan_key, plan_key), periods=periods))
    return CatalogOut(web_enabled=mp.is_configured(), android_enabled=gp.is_configured(), plans=plans)


@router.get("/subscription", response_model=AccountBillingOut)
async def get_account_billing(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    now = datetime.now(timezone.utc)
    subs = await billing.user_subscriptions(db, current_user.id)
    current = billing._best(subs, now)
    upcoming = next((
        s for s in sorted(subs, key=lambda s: s.created_at, reverse=True)
        if s is not current and s.current_period_start is not None
        and billing._utc(s.current_period_start) > now
        and (s.status in ("active", "grace") or (s.status == "pending" and s.auto_renew))
    ), None)
    renewing = next((s for s in subs if billing.is_renewing(s)), None)
    return AccountBillingOut(
        plan=current_user.plan, effective_plan=_effective_plan(current_user),
        plan_expires_at=current_user.plan_expires_at,
        current=_sub_out(current) if current else None,
        upcoming=_sub_out(upcoming) if upcoming else None,
        managed_by=billing.managed_by(renewing or current),
        play_manage_url=billing.play_manage_url(),
    )


@router.get("/subscriptions/{subscription_id}", response_model=SubscriptionOut)
async def get_subscription(
    subscription_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    sub = (
        await db.execute(select(Subscription).where(
            Subscription.id == subscription_id, Subscription.user_id == current_user.id,
        ))
    ).scalar_one_or_none()
    if sub is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Assinatura não encontrada")
    return _sub_out(sub)


@router.post("/checkout", response_model=CheckoutOut, status_code=status.HTTP_201_CREATED)
async def start_checkout(
    body: CheckoutIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if body.kind == "credits":
        if body.package_id is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Informe o pacote")
        purchase = await credits.start_purchase(credits.PurchaseIn(package_id=body.package_id), current_user, db)
        return CheckoutOut(kind="credits", id=purchase.id, status=purchase.status, checkout_url=purchase.checkout_url)
    if body.plan_key is None or body.period is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Informe o plano e o período")
    try:
        sub = await billing.start_web_subscription(db, current_user, body.plan_key, body.period, body.method)
    except billing.BillingError as exc:
        raise _http(exc)
    return CheckoutOut(kind="subscription", id=sub.id, status=sub.status, checkout_url=sub.checkout_url)


@router.post("/subscription/cancel", response_model=SubscriptionOut)
async def cancel_subscription(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    try:
        return _sub_out(await billing.cancel_subscription(db, current_user))
    except billing.BillingError as exc:
        raise _http(exc)


@router.post("/google/verify", response_model=GoogleVerifyOut)
async def verify_google_purchase(
    body: GoogleVerifyIn,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    if not gp.is_configured():
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail={"code": "payments_unavailable", "message": "Compra ainda não disponível."})
    try:
        if body.kind == "subscription":
            data = await gp.get_subscription(body.purchase_token)
            sub = await billing.apply_play_subscription(db, body.purchase_token, data, expected_user=current_user)
            return GoogleVerifyOut(kind="subscription", subscription=_sub_out(sub))
        data = await gp.get_product_purchase(body.product_id, body.purchase_token)
        purchase = await billing.apply_play_product(
            db, body.product_id, body.purchase_token, data, expected_user=current_user,
        )
        return GoogleVerifyOut(kind="credits", purchase=credits._purchase_out(purchase))
    except billing.BillingError as exc:
        raise _http(exc)
    except gp.GooglePlayError:
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY,
                            detail={"code": "verify_failed", "message": "Não foi possível confirmar a compra. Tente de novo."})


@router.post("/webhooks/mercadopago", status_code=status.HTTP_204_NO_CONTENT)
async def mercadopago_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    params = request.query_params
    try:
        body = json.loads(await request.body() or b"{}")
    except ValueError:
        body = {}
    if not isinstance(body, dict):
        body = {}
    topic = params.get("type") or params.get("topic") or body.get("type") or body.get("topic") or ""
    data_id = params.get("data.id") or str((body.get("data") or {}).get("id") or "") or params.get("id") or ""
    if not mp.verify_signature(request.headers.get("x-signature"), request.headers.get("x-request-id"), data_id):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Assinatura inválida")
    if not data_id:
        return
    event_id = f"{topic}:{data_id}:{body.get('id') or request.headers.get('x-request-id') or uuid.uuid4()}"
    if not await billing.begin_event(db, "mercadopago", event_id, topic or "unknown", body):
        return
    try:
        await billing.handle_mp_notification(db, topic, data_id)
    except mp.MercadoPagoError:
        # Sem resposta 2xx o Mercado Pago reenvia a notificacao.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Mercado Pago indisponível")
    await billing.finish_event(db, "mercadopago", event_id)


@router.post("/webhooks/google-play", status_code=status.HTTP_204_NO_CONTENT)
async def google_play_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    if not await gp.verify_pubsub_push(request.headers.get("authorization")):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Push não autorizado")
    try:
        message_id, payload = billing.decode_pubsub_message(await request.json())
    except (billing.BillingError, ValueError):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Mensagem inválida")
    kind = next((k for k in payload if k.endswith("Notification")), "unknown")
    event_id = message_id or str(uuid.uuid4())
    if not await billing.begin_event(db, "google_play", event_id, kind, payload):
        return
    try:
        await billing.handle_play_notification(db, payload)
    except gp.GooglePlayError:
        # Sem resposta 2xx o Pub/Sub reenvia a mensagem.
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail="Google Play indisponível")
    except billing.BillingError as exc:
        logger.warning("Notificação do Play %s ignorada: %s", event_id, exc.detail())
    await billing.finish_event(db, "google_play", event_id)
