"""
Assinaturas e compras: regras comuns ao Mercado Pago (webapp) e ao Google Play (Android).

- O plano e da conta: user.plan / plan_expires_at sao derivados da assinatura valida
  (sync_user_plan), e os limites de subscription.py seguem iguais.
- Uma assinatura paga por vez. Upgrade vale na hora; downgrade, troca de periodo e
  renovacao por Pix comecam no fim do periodo atual. Cancelar desliga a renovacao e
  mantem o periodo ja pago.
- Reembolso/chargeback derruba o plano na hora; nos creditos, retira o que sobrou do lote.
- O estado vem sempre da API do provedor, nunca do corpo da notificacao; reprocessar
  o mesmo evento nao muda nada (idempotente).
"""
from __future__ import annotations

import base64
import binascii
import calendar
import json
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models.billing import ACCESS_STATUSES, PLAN_PERIODS, PaymentEvent, PlanPrice, Subscription
from app.models.credits import CreditPackage, CreditPurchase
from app.models.user import User
from app.services import google_play as gp
from app.services import mercadopago as mp
from app.services.credits import apply_purchase_status, revoke_purchase_lot

logger = logging.getLogger(__name__)

PLAN_RANK = {"free": 0, "jardineiro": 1, "cultivador": 2, "grower_pro": 3}
PERIOD_LABEL = {"monthly": "mensal", "yearly": "anual"}
# Assinaturas que ainda renovam (ou podem voltar a renovar) no provedor
RENEWING_STATUSES = ("active", "grace", "on_hold", "paused")
# Checkout pendente por mais tempo que isso e dado como abandonado
CHECKOUT_TTL = timedelta(days=3)

PLAY_SUBSCRIPTION_STATES = {
    "SUBSCRIPTION_STATE_ACTIVE": "active",
    "SUBSCRIPTION_STATE_IN_GRACE_PERIOD": "grace",
    "SUBSCRIPTION_STATE_ON_HOLD": "on_hold",
    "SUBSCRIPTION_STATE_PAUSED": "paused",
    "SUBSCRIPTION_STATE_CANCELED": "canceled",
    "SUBSCRIPTION_STATE_EXPIRED": "expired",
    "SUBSCRIPTION_STATE_PENDING": "pending",
    "SUBSCRIPTION_STATE_PENDING_PURCHASE_CANCELED": "failed",
}
# purchases.products.purchaseState
PLAY_PRODUCT_STATES = {0: "paid", 1: "canceled", 2: "pending"}
# RTDN subscriptionNotification.notificationType
PLAY_SUBSCRIPTION_REVOKED = 12
# RTDN oneTimeProductNotification.notificationType
PLAY_ONE_TIME_PURCHASED, PLAY_ONE_TIME_CANCELED = 1, 2
# voidedPurchaseNotification.productType
PLAY_VOIDED_SUBSCRIPTION = 1


class BillingError(Exception):
    def __init__(self, status_code: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.extra = extra

    def detail(self) -> dict:
        return {"code": self.code, "message": self.message, **self.extra}


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def add_months(value: datetime, months: int) -> datetime:
    index = value.month - 1 + months
    year, month = value.year + index // 12, index % 12 + 1
    return value.replace(year=year, month=month, day=min(value.day, calendar.monthrange(year, month)[1]))


def access_until(sub: Subscription) -> datetime | None:
    """Ate quando a assinatura da acesso ao plano (None = nao da acesso)."""
    end = _utc(sub.current_period_end)
    if sub.status not in ACCESS_STATUSES or end is None:
        return None
    if sub.provider == "mercadopago" and sub.kind == "recurring" and sub.status == "active":
        # O MP tenta cobrar de novo por alguns dias depois da data de renovacao.
        end += timedelta(days=settings.BILLING_GRACE_DAYS)
    return end


def grants_access(sub: Subscription, now: datetime) -> bool:
    end, start = access_until(sub), _utc(sub.current_period_start)
    return end is not None and end > now and (start is None or start <= now)


def _best(subs: list[Subscription], now: datetime) -> Subscription | None:
    valid = [s for s in subs if grants_access(s, now)]
    if not valid:
        return None
    return max(valid, key=lambda s: (PLAN_RANK.get(s.plan_key, 0), access_until(s)))


def managed_by(sub: Subscription | None) -> str | None:
    if sub is None:
        return None
    return "google_play" if sub.provider == "google_play" else "web"


def is_renewing(sub: Subscription) -> bool:
    return sub.auto_renew and sub.status in RENEWING_STATUSES


async def user_subscriptions(db: AsyncSession, user_id: uuid.UUID) -> list[Subscription]:
    return list((
        await db.execute(
            select(Subscription).where(Subscription.user_id == user_id).order_by(Subscription.created_at)
        )
    ).scalars().all())


async def current_subscription(db: AsyncSession, user: User, now: datetime | None = None) -> Subscription | None:
    return _best(await user_subscriptions(db, user.id), now or datetime.now(timezone.utc))


async def sync_user_plan(db: AsyncSession, user: User, now: datetime | None = None) -> None:
    """Grava em user.plan o plano da assinatura valida de maior nivel.

    Sem assinatura valida, so volta ao Free quem esta num plano que veio de uma
    assinatura: plano dado a mao pelo admin (sem assinatura) nao e tocado.
    """
    now = now or datetime.now(timezone.utc)
    subs = await user_subscriptions(db, user.id)
    if not subs:
        return
    best = _best(subs, now)
    if best is not None:
        user.plan = best.plan_key
        user.plan_expires_at = access_until(best)
    elif user.plan in {s.plan_key for s in subs}:
        user.plan = "free"
        user.plan_expires_at = None
    await db.commit()


# ── Catalogo ──────────────────────────────────────────────────────────────────

async def plan_labels(db: AsyncSession) -> dict[str, str]:
    from app.services.plan_config_service import get_all_plan_configs
    return {key: cfg["label"] for key, cfg in (await get_all_plan_configs(db)).items()}


async def plan_prices(db: AsyncSession) -> list[PlanPrice]:
    return list((await db.execute(select(PlanPrice))).scalars().all())


def is_sellable(price: PlanPrice | None) -> bool:
    return price is not None and price.is_active and price.price_brl is not None and float(price.price_brl) > 0


async def _price(db: AsyncSession, plan_key: str, period: str) -> PlanPrice | None:
    return (
        await db.execute(select(PlanPrice).where(PlanPrice.plan_key == plan_key, PlanPrice.period == period))
    ).scalar_one_or_none()


# ── Mercado Pago (webapp) ─────────────────────────────────────────────────────

def _checkout_url(data: dict) -> str | None:
    if settings.MP_ACCESS_TOKEN.startswith("TEST-") and data.get("sandbox_init_point"):
        return data["sandbox_init_point"]
    return data.get("init_point")


async def start_web_subscription(
    db: AsyncSession, user: User, plan_key: str, period: str, method: str, now: datetime | None = None,
) -> Subscription:
    """Cartao = assinatura recorrente (preapproval); Pix = um periodo pago adiantado."""
    now = now or datetime.now(timezone.utc)
    if plan_key not in PLAN_RANK or plan_key == "free" or period not in PLAN_PERIODS:
        raise BillingError(422, "invalid_plan", "Plano ou período inválido.")
    price = await _price(db, plan_key, period)
    if not is_sellable(price):
        raise BillingError(404, "plan_not_for_sale", "Este plano ainda não está à venda.")
    if not mp.is_configured():
        raise BillingError(503, "payments_unavailable", "Pagamento ainda não disponível.")

    subs = await user_subscriptions(db, user.id)
    if any(s.provider == "google_play" and is_renewing(s) for s in subs):
        raise BillingError(409, "managed_by_google_play",
                           "Sua assinatura é gerenciada pelo Google Play, no app Android.")
    if method == "card" and any(
        s.provider == "mercadopago" and s.kind == "recurring" and is_renewing(s)
        and s.plan_key == plan_key and s.period == period for s in subs
    ):
        raise BillingError(409, "already_subscribed", "Você já assina este plano.")

    current = _best(subs, now)
    starts_at = now
    if current is not None and PLAN_RANK[plan_key] <= PLAN_RANK.get(current.plan_key, 0):
        # Mesmo plano ou inferior: comeca quando o periodo atual termina.
        starts_at = max(now, _utc(current.current_period_end) or now)

    labels = await plan_labels(db)
    title = f"LetsGrow {labels.get(plan_key, plan_key)} · {PERIOD_LABEL[period]}"
    sub = Subscription(
        id=uuid.uuid4(), user_id=user.id, plan_key=plan_key, period=period, provider="mercadopago",
        kind="recurring" if method == "card" else "prepaid", price_brl=price.price_brl, status="pending",
        # auto_renew so liga quando o MP confirma a autorizacao do cartao (sync_preapproval).
        auto_renew=False, current_period_start=starts_at if starts_at > now else None,
        created_at=now, updated_at=now,
    )
    try:
        if method == "card":
            data = await mp.create_preapproval(
                external_reference=f"sub:{sub.id}", reason=title, amount=float(price.price_brl),
                months=PLAN_PERIODS[period], payer_email=user.email, ref_id=str(sub.id),
                start_date=starts_at if starts_at > now + timedelta(minutes=5) else None,
            )
            sub.provider_ref = str(data["id"])
        else:
            data = await mp.create_preference(
                external_reference=f"period:{sub.id}", title=title, amount=float(price.price_brl),
                payer_email=user.email, kind="subscription", ref_id=str(sub.id), pix_only=True,
            )
    except mp.MercadoPagoError:
        raise BillingError(502, "checkout_failed", "Não foi possível iniciar o pagamento. Tente de novo.")
    sub.checkout_url = _checkout_url(data)
    db.add(sub)
    await db.commit()
    return sub


async def cancel_subscription(db: AsyncSession, user: User, now: datetime | None = None) -> Subscription:
    """Desliga a renovacao; o periodo pago continua valendo ate o fim."""
    now = now or datetime.now(timezone.utc)
    subs = [s for s in await user_subscriptions(db, user.id) if is_renewing(s) or (s.auto_renew and s.status == "pending")]
    if not subs:
        raise BillingError(404, "no_subscription", "Nenhuma assinatura com renovação ativa.")
    play = next((s for s in subs if s.provider == "google_play"), None)
    if play is not None:
        raise BillingError(409, "manage_in_google_play", "Cancele pelo Google Play, no app Android.",
                           manage_url=play_manage_url())
    for sub in subs:
        await _cancel_mp_recurring(sub, now)
    await db.commit()
    await sync_user_plan(db, user, now)
    return max(subs, key=lambda s: PLAN_RANK.get(s.plan_key, 0))


def play_manage_url() -> str:
    return f"https://play.google.com/store/account/subscriptions?package={settings.GOOGLE_PLAY_PACKAGE_NAME}"


async def _cancel_mp_recurring(sub: Subscription, now: datetime) -> None:
    if sub.provider_ref:
        try:
            await mp.cancel_preapproval(sub.provider_ref)
        except mp.MercadoPagoError:
            raise BillingError(502, "cancel_failed", "Não foi possível cancelar agora. Tente de novo.")
    sub.auto_renew = False
    sub.status = "canceled"
    sub.canceled_at = sub.canceled_at or now
    sub.updated_at = now


def _paid_enough(payment: dict, expected: float | None) -> bool:
    if expected is None:
        return True
    return float(payment.get("transaction_amount") or 0) + 0.01 >= float(expected)


async def refund_credit_purchase(db: AsyncSession, purchase: CreditPurchase, new_status: str,
                                 now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    if purchase.status in ("refunded", "charged_back"):
        return
    if purchase.status == "paid" and purchase.user_id is not None:
        label = "Estorno" if new_status == "charged_back" else "Reembolso"
        await revoke_purchase_lot(db, purchase.user_id, purchase.id,
                                  f"{label} · {purchase.label}", now)
    purchase.status = new_status if purchase.status == "paid" else "canceled"
    purchase.updated_at = now
    await db.commit()


async def _refund_subscription(db: AsyncSession, sub: Subscription, now: datetime, cancel_remote: bool) -> None:
    if sub.status == "refunded":
        return
    if cancel_remote and sub.provider == "mercadopago" and sub.kind == "recurring" and sub.provider_ref and sub.auto_renew:
        try:
            await mp.cancel_preapproval(sub.provider_ref)
        except mp.MercadoPagoError:
            logger.error("Reembolso: preapproval %s não foi cancelado no MP", sub.provider_ref)
    sub.status = "refunded"
    sub.auto_renew = False
    sub.ended_at = now
    sub.updated_at = now
    await db.commit()
    await _sync_owner(db, sub, now)


async def _sync_owner(db: AsyncSession, sub: Subscription, now: datetime) -> None:
    if sub.user_id is not None:
        user = await db.get(User, sub.user_id)
        if user is not None:
            await sync_user_plan(db, user, now)


def _parse_reference(value: str | None) -> tuple[str, uuid.UUID | None]:
    kind, _, raw = (value or "").partition(":")
    try:
        return kind, uuid.UUID(raw)
    except ValueError:
        return kind, None


async def process_mp_payment(db: AsyncSession, payment: dict, now: datetime | None = None) -> None:
    """Pagamento do MP (creditos, periodo por Pix ou cobranca de uma assinatura)."""
    now = now or datetime.now(timezone.utc)
    kind, ref_id = _parse_reference(payment.get("external_reference"))
    status = payment.get("status")
    if ref_id is None:
        logger.info("Pagamento MP %s sem referência do LetsGrow", payment.get("id"))
        return

    if kind == "credits":
        purchase = await db.get(CreditPurchase, ref_id)
        if purchase is None:
            return
        if status == "approved":
            if not _paid_enough(payment, purchase.price_brl):
                logger.error("Pagamento MP %s abaixo do preço da compra %s", payment.get("id"), purchase.id)
                return
            await apply_purchase_status(db, purchase, "paid")
        elif status in ("rejected", "cancelled"):
            await apply_purchase_status(db, purchase, "failed" if status == "rejected" else "canceled")
        elif status in ("refunded", "charged_back"):
            await refund_credit_purchase(db, purchase, status, now)
        return

    sub = await db.get(Subscription, ref_id)
    if sub is None:
        return
    if kind == "period":
        if status == "approved" and sub.status == "pending":
            if not _paid_enough(payment, sub.price_brl):
                logger.error("Pagamento MP %s abaixo do preço da assinatura %s", payment.get("id"), sub.id)
                return
            start = max(_utc(sub.current_period_start) or now, now)
            sub.current_period_start = start
            sub.current_period_end = add_months(start, PLAN_PERIODS[sub.period])
            sub.status = "active"
            sub.provider_ref = sub.last_payment_ref = str(payment.get("id"))
            sub.updated_at = now
            await db.commit()
            await _sync_owner(db, sub, now)
        elif status in ("rejected", "cancelled") and sub.status == "pending":
            sub.status = "failed"
            sub.updated_at = now
            await db.commit()
        elif status in ("refunded", "charged_back"):
            await _refund_subscription(db, sub, now, cancel_remote=False)
    elif kind == "sub":
        if status in ("refunded", "charged_back"):
            await _refund_subscription(db, sub, now, cancel_remote=True)
        elif sub.provider_ref:
            await sync_preapproval(db, await mp.get_preapproval(sub.provider_ref), now)


async def sync_preapproval(db: AsyncSession, data: dict, now: datetime | None = None) -> None:
    """Estado da assinatura no cartao a partir do preapproval do MP."""
    now = now or datetime.now(timezone.utc)
    kind, ref_id = _parse_reference(data.get("external_reference"))
    sub = await db.get(Subscription, ref_id) if kind == "sub" and ref_id else None
    if sub is None or sub.status == "refunded":
        return
    sub.provider_ref = str(data.get("id") or sub.provider_ref)
    status = data.get("status")
    summary = data.get("summarized") or {}
    last_charged = mp.parse_datetime(summary.get("last_charged_date"))
    if status == "authorized":
        sub.auto_renew = True
        if summary.get("charged_quantity") and last_charged is not None:
            sub.current_period_start = last_charged
            sub.current_period_end = add_months(last_charged, PLAN_PERIODS[sub.period])
            sub.status = "active"
        # A nova assinatura no cartao substitui as outras que ainda renovariam.
        if sub.user_id is not None:
            for other in await user_subscriptions(db, sub.user_id):
                if (other.id != sub.id and other.provider == "mercadopago" and other.kind == "recurring"
                        and other.auto_renew and other.status in ("active", "grace", "pending")):
                    try:
                        await _cancel_mp_recurring(other, now)
                    except BillingError:
                        logger.error("Assinatura %s não foi cancelada no MP após a troca", other.id)
    elif status == "paused":
        sub.status = "paused"
    elif status == "cancelled":
        sub.auto_renew = False
        sub.status = "canceled"
        sub.canceled_at = sub.canceled_at or now
    sub.updated_at = now
    await db.commit()
    await _sync_owner(db, sub, now)


async def handle_mp_notification(db: AsyncSession, topic: str, resource_id: str, now: datetime | None = None) -> None:
    now = now or datetime.now(timezone.utc)
    if topic == "payment":
        await process_mp_payment(db, await mp.get_payment(resource_id), now)
    elif topic in ("subscription_preapproval", "preapproval"):
        await sync_preapproval(db, await mp.get_preapproval(resource_id), now)
    elif topic == "subscription_authorized_payment":
        charge = await mp.get_authorized_payment(resource_id)
        payment = charge.get("payment") or {}
        if charge.get("preapproval_id"):
            if payment.get("status") in ("refunded", "charged_back"):
                sub = await _by_ref(db, Subscription, "mercadopago", str(charge["preapproval_id"]))
                if sub is not None:
                    await _refund_subscription(db, sub, now, cancel_remote=True)
                return
            await sync_preapproval(db, await mp.get_preapproval(str(charge["preapproval_id"])), now)


# ── Google Play (Android) ─────────────────────────────────────────────────────

async def _by_ref(db: AsyncSession, model, provider: str, ref: str):
    return (
        await db.execute(select(model).where(model.provider == provider, model.provider_ref == ref))
    ).scalars().first()


async def _account_user(db: AsyncSession, account_id: str | None) -> User | None:
    """O app envia o id da conta como obfuscatedAccountId (UUID aleatorio, sem dado pessoal)."""
    try:
        return await db.get(User, uuid.UUID(account_id)) if account_id else None
    except ValueError:
        return None


def _check_owner(owner: User | None, expected: User | None) -> None:
    if expected is not None and (owner is None or owner.id != expected.id):
        raise BillingError(403, "purchase_account_mismatch", "Esta compra pertence a outra conta.")


async def apply_play_subscription(
    db: AsyncSession, token: str, data: dict, now: datetime | None = None, *,
    expected_user: User | None = None, revoked: bool = False,
) -> Subscription:
    now = now or datetime.now(timezone.utc)
    owner = await _account_user(db, (data.get("externalAccountIdentifiers") or {}).get("obfuscatedExternalAccountId"))
    _check_owner(owner, expected_user)
    lines = data.get("lineItems") or []
    if not lines:
        raise BillingError(422, "invalid_purchase", "Compra sem itens.")
    line = max(lines, key=lambda item: item.get("expiryTime") or "")
    product_id = line.get("productId")
    base_plan = (line.get("offerDetails") or {}).get("basePlanId")
    price = (
        await db.execute(select(PlanPrice).where(
            PlanPrice.google_product_id == product_id, PlanPrice.google_base_plan_id == base_plan,
        ))
    ).scalars().first()
    if price is None:
        raise BillingError(422, "unknown_product", "Produto do Google Play não cadastrado no admin.",
                           product_id=product_id, base_plan_id=base_plan)

    sub = await _by_ref(db, Subscription, "google_play", token)
    if sub is None:
        sub = Subscription(id=uuid.uuid4(), provider="google_play", kind="recurring", provider_ref=token,
                           created_at=now)
        db.add(sub)
    if sub.user_id is None and owner is not None:
        sub.user_id = owner.id
    sub.plan_key, sub.period = price.plan_key, price.period
    status = PLAY_SUBSCRIPTION_STATES.get(data.get("subscriptionState"), "pending")
    if revoked or sub.status == "refunded":
        status = "refunded"
        sub.ended_at = sub.ended_at or now
    sub.status = status
    sub.auto_renew = status != "refunded" and bool((line.get("autoRenewingPlan") or {}).get("autoRenewEnabled"))
    sub.current_period_start = _utc(sub.current_period_start) or gp.parse_time(data.get("startTime")) or now
    sub.current_period_end = gp.parse_time(line.get("expiryTime"))
    if status == "canceled":
        sub.canceled_at = sub.canceled_at or now
    sub.last_payment_ref = (data.get("latestOrderId") or "")[:100] or sub.last_payment_ref
    sub.updated_at = now

    # Upgrade/downgrade no Play gera um token novo que aponta para o anterior.
    linked = data.get("linkedPurchaseToken")
    if linked:
        old = await _by_ref(db, Subscription, "google_play", linked)
        if old is not None and old.id != sub.id and old.status != "refunded":
            old.status, old.auto_renew, old.ended_at, old.updated_at = "replaced", False, now, now
    await db.commit()

    if data.get("acknowledgementState") == "ACKNOWLEDGEMENT_STATE_PENDING" and status in ("active", "grace"):
        try:
            # Sem acknowledge em 3 dias o Google reembolsa a compra.
            await gp.acknowledge_subscription(product_id, token)
        except gp.GooglePlayError:
            logger.error("Acknowledge da assinatura %s falhou; nova tentativa na reconciliação", sub.id)
    await _sync_owner(db, sub, now)
    return sub


async def apply_play_product(
    db: AsyncSession, product_id: str, token: str, data: dict, now: datetime | None = None, *,
    expected_user: User | None = None,
) -> CreditPurchase:
    now = now or datetime.now(timezone.utc)
    owner = await _account_user(db, data.get("obfuscatedExternalAccountId"))
    _check_owner(owner, expected_user)
    package = (
        await db.execute(select(CreditPackage).where(CreditPackage.google_product_id == product_id))
    ).scalar_one_or_none()
    if package is None:
        raise BillingError(422, "unknown_product", "Pacote do Google Play não cadastrado no admin.",
                           product_id=product_id)
    purchase = await _by_ref(db, CreditPurchase, "google_play", token)
    if purchase is None:
        purchase = CreditPurchase(
            id=uuid.uuid4(), user_id=owner.id if owner else None, package_id=package.id, label=package.label,
            credits=package.credits, price_brl=package.price_brl, validity_days=package.validity_days,
            provider="google_play", provider_ref=token, status="pending", created_at=now, updated_at=now,
        )
        db.add(purchase)
        await db.commit()
    state = PLAY_PRODUCT_STATES.get(data.get("purchaseState"), "pending")
    if state != "pending":
        await apply_purchase_status(db, purchase, state)
    if purchase.status == "paid" and data.get("consumptionState") == 0:
        try:
            # Consumir libera o mesmo pacote para nova compra; os creditos ja estao no saldo.
            await gp.consume_product(product_id, token)
        except gp.GooglePlayError:
            logger.error("Consume da compra %s falhou; o app tenta de novo ao reabrir", purchase.id)
    return purchase


async def handle_play_notification(db: AsyncSession, payload: dict, now: datetime | None = None) -> None:
    """Corpo decodificado de uma Real-time Developer Notification."""
    now = now or datetime.now(timezone.utc)
    if payload.get("packageName") not in (None, settings.GOOGLE_PLAY_PACKAGE_NAME):
        return
    if (note := payload.get("subscriptionNotification")) is not None:
        token = note["purchaseToken"]
        await apply_play_subscription(
            db, token, await gp.get_subscription(token), now,
            revoked=note.get("notificationType") == PLAY_SUBSCRIPTION_REVOKED,
        )
    elif (note := payload.get("oneTimeProductNotification")) is not None:
        token, sku = note["purchaseToken"], note.get("sku")
        if note.get("notificationType") == PLAY_ONE_TIME_PURCHASED and sku:
            await apply_play_product(db, sku, token, await gp.get_product_purchase(sku, token), now)
        elif note.get("notificationType") == PLAY_ONE_TIME_CANCELED:
            purchase = await _by_ref(db, CreditPurchase, "google_play", token)
            if purchase is not None:
                await apply_purchase_status(db, purchase, "canceled")
    elif (note := payload.get("voidedPurchaseNotification")) is not None:
        token = note["purchaseToken"]
        if note.get("productType") == PLAY_VOIDED_SUBSCRIPTION:
            sub = await _by_ref(db, Subscription, "google_play", token)
            if sub is not None:
                await _refund_subscription(db, sub, now, cancel_remote=False)
        else:
            purchase = await _by_ref(db, CreditPurchase, "google_play", token)
            if purchase is not None:
                await refund_credit_purchase(db, purchase, "refunded", now)


def decode_pubsub_message(body: dict) -> tuple[str, dict]:
    message = body.get("message") or {}
    try:
        payload = json.loads(base64.b64decode(message.get("data") or ""))
    except (binascii.Error, ValueError):
        raise BillingError(400, "invalid_message", "Mensagem do Pub/Sub inválida.")
    return str(message.get("messageId") or message.get("message_id") or ""), payload


# ── Eventos (idempotencia) ────────────────────────────────────────────────────

async def begin_event(db: AsyncSession, provider: str, event_id: str, kind: str, payload: dict | None) -> bool:
    """Registra o evento; False se ja foi processado (notificacao repetida)."""
    existing = (
        await db.execute(select(PaymentEvent).where(PaymentEvent.provider == provider, PaymentEvent.event_id == event_id))
    ).scalar_one_or_none()
    if existing is not None:
        return existing.processed_at is None
    db.add(PaymentEvent(id=uuid.uuid4(), provider=provider, event_id=event_id[:200], kind=kind[:60],
                        payload=payload, received_at=datetime.now(timezone.utc)))
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        return False
    return True


async def finish_event(db: AsyncSession, provider: str, event_id: str) -> None:
    event = (
        await db.execute(select(PaymentEvent).where(PaymentEvent.provider == provider, PaymentEvent.event_id == event_id[:200]))
    ).scalar_one_or_none()
    if event is not None:
        event.processed_at = datetime.now(timezone.utc)
        await db.commit()


# ── Reconciliacao (scheduler) ─────────────────────────────────────────────────

async def reconcile(db: AsyncSession, now: datetime | None = None) -> dict:
    """Corrige o que um webhook perdido deixaria errado. Cada item falha sozinho."""
    now = now or datetime.now(timezone.utc)
    stats = {"payments": 0, "subscriptions": 0, "abandoned": 0, "users": 0, "errors": 0}

    async def guarded(coro) -> None:
        try:
            await coro
        except Exception as exc:  # um item com erro nao para a reconciliacao
            await db.rollback()
            stats["errors"] += 1
            logger.warning("Reconciliação de pagamento: %s", exc)

    recent = now - timedelta(minutes=10)
    stale = now - CHECKOUT_TTL
    if mp.is_configured():
        purchases = (await db.execute(select(CreditPurchase).where(
            CreditPurchase.provider == "mercadopago", CreditPurchase.status == "pending",
            CreditPurchase.created_at < recent,
        ))).scalars().all()
        for purchase in purchases:
            if _utc(purchase.created_at) < stale:
                purchase.status = "canceled"
                stats["abandoned"] += 1
                await db.commit()
                continue
            await guarded(_reconcile_reference(db, f"credits:{purchase.id}", now))
            stats["payments"] += 1
        subs = (await db.execute(select(Subscription).where(
            Subscription.provider == "mercadopago", Subscription.created_at < recent,
            Subscription.status.in_(("pending", "active", "grace")),
        ))).scalars().all()
        for sub in subs:
            # Pendente sem autorizacao (auto_renew desligado) = checkout abandonado. Um cartao ja
            # autorizado para comecar depois (downgrade) fica pendente com auto_renew ligado.
            if (sub.status == "pending" and not sub.auto_renew and _utc(sub.created_at) < stale
                    and sub.current_period_end is None):
                if sub.kind == "recurring" and sub.provider_ref:
                    await guarded(_cancel_mp_recurring(sub, now))
                sub.status, sub.auto_renew = "failed", False
                stats["abandoned"] += 1
                await db.commit()
            elif sub.kind == "prepaid":
                if sub.status == "pending":
                    await guarded(_reconcile_reference(db, f"period:{sub.id}", now))
            elif sub.provider_ref:
                await guarded(_resync_preapproval(db, sub.provider_ref, now))
            stats["subscriptions"] += 1

    if gp.is_configured():
        play = (await db.execute(select(Subscription).where(
            Subscription.provider == "google_play",
            Subscription.status.in_(("pending", "active", "grace", "on_hold", "paused", "canceled")),
        ))).scalars().all()
        for sub in play:
            end = _utc(sub.current_period_end)
            if sub.status in ("pending", "on_hold", "paused") or end is None or end < now + timedelta(days=1):
                await guarded(_resync_play(db, sub.provider_ref, now))
                stats["subscriptions"] += 1

    # Inicio adiado (downgrade/renovacao por Pix) e fim de periodo sem evento do provedor.
    user_ids = {row for row in (await db.execute(
        select(Subscription.user_id).where(Subscription.user_id.is_not(None), Subscription.status.in_(ACCESS_STATUSES))
    )).scalars().all()}
    for user_id in user_ids:
        user = await db.get(User, user_id)
        if user is not None:
            await guarded(sync_user_plan(db, user, now))
            stats["users"] += 1
    return stats


async def _reconcile_reference(db: AsyncSession, reference: str, now: datetime) -> None:
    payments = await mp.search_payments(reference)
    chosen = next((p for p in payments if p.get("status") in ("refunded", "charged_back")), None) \
        or next((p for p in payments if p.get("status") == "approved"), None)
    if chosen is not None:
        await process_mp_payment(db, chosen, now)


async def _resync_preapproval(db: AsyncSession, preapproval_id: str, now: datetime) -> None:
    await sync_preapproval(db, await mp.get_preapproval(preapproval_id), now)


async def _resync_play(db: AsyncSession, token: str | None, now: datetime) -> None:
    if token:
        await apply_play_subscription(db, token, await gp.get_subscription(token), now)
