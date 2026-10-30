"""
Admin de pagamentos: precos dos planos por periodo, assinaturas e estado dos provedores.

GET  /admin/billing/status                         – provedores configurados (sem expor segredos)
GET  /admin/billing/plan-prices                    – plano x periodo (linhas ausentes vem vazias)
PUT  /admin/billing/plan-prices/{plan_key}/{period} – preco web, ativo e IDs do Google Play
GET  /admin/billing/subscriptions?email=           – assinaturas de uma conta (ou as mais recentes)
"""
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.admin import require_admin_token
from app.config import settings
from app.database import get_db
from app.models.billing import PLAN_PERIODS, PlanPrice, Subscription
from app.models.user import User
from app.services import billing
from app.services import google_play as gp
from app.services import mercadopago as mp

router = APIRouter(dependencies=[Depends(require_admin_token)])

PAID_PLAN_KEYS = ("jardineiro", "cultivador", "grower_pro")


class BillingStatusOut(BaseModel):
    payment_provider: str
    mercadopago_configured: bool
    mercadopago_sandbox: bool
    mercadopago_webhook_secret: bool
    web_app_url: str
    google_play_configured: bool
    google_play_package: str
    google_play_rtdn_configured: bool


class PlanPriceOut(BaseModel):
    plan_key: str
    label: str
    period: str
    price_brl: float | None
    is_active: bool
    google_product_id: str | None
    google_base_plan_id: str | None
    updated_by: str | None
    updated_at: datetime | None


class PlanPriceIn(BaseModel):
    price_brl: float | None = Field(default=None, gt=0, le=100_000)
    is_active: bool = False
    google_product_id: str | None = Field(default=None, min_length=1, max_length=100)
    google_base_plan_id: str | None = Field(default=None, min_length=1, max_length=100)
    updated_by: str | None = Field(default=None, max_length=255)

    model_config = {"extra": "forbid"}


class AdminSubscriptionOut(BaseModel):
    id: uuid.UUID
    user_email: str | None
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
    canceled_at: datetime | None
    ended_at: datetime | None
    created_at: datetime


@router.get("/billing/status", response_model=BillingStatusOut)
async def billing_status():
    return BillingStatusOut(
        payment_provider=settings.PAYMENT_PROVIDER,
        mercadopago_configured=mp.is_configured(),
        mercadopago_sandbox=settings.MP_ACCESS_TOKEN.startswith("TEST-"),
        mercadopago_webhook_secret=bool(settings.MP_WEBHOOK_SECRET),
        web_app_url=settings.WEB_APP_URL,
        google_play_configured=gp.is_configured(),
        google_play_package=settings.GOOGLE_PLAY_PACKAGE_NAME,
        google_play_rtdn_configured=bool(settings.GOOGLE_PLAY_RTDN_AUDIENCE and settings.GOOGLE_PLAY_RTDN_SERVICE_ACCOUNT),
    )


@router.get("/billing/plan-prices", response_model=list[PlanPriceOut])
async def list_plan_prices(db: AsyncSession = Depends(get_db)):
    labels = await billing.plan_labels(db)
    rows = {(p.plan_key, p.period): p for p in await billing.plan_prices(db)}
    out = []
    for plan_key in PAID_PLAN_KEYS:
        for period in PLAN_PERIODS:
            row = rows.get((plan_key, period))
            out.append(PlanPriceOut(
                plan_key=plan_key, label=labels.get(plan_key, plan_key), period=period,
                price_brl=float(row.price_brl) if row and row.price_brl is not None else None,
                is_active=bool(row and row.is_active),
                google_product_id=row.google_product_id if row else None,
                google_base_plan_id=row.google_base_plan_id if row else None,
                updated_by=row.updated_by if row else None,
                updated_at=row.updated_at if row else None,
            ))
    return out


@router.put("/billing/plan-prices/{plan_key}/{period}", response_model=PlanPriceOut)
async def put_plan_price(plan_key: str, period: str, body: PlanPriceIn, db: AsyncSession = Depends(get_db)):
    if plan_key not in PAID_PLAN_KEYS or period not in PLAN_PERIODS:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Plano ou período inválido")
    if body.is_active and body.price_brl is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Defina o preço antes de ativar a venda")
    if bool(body.google_product_id) != bool(body.google_base_plan_id):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                            detail="Informe o ID da assinatura e o do base plan do Google Play juntos")
    if body.google_product_id:
        clash = (await db.execute(select(PlanPrice).where(
            PlanPrice.google_product_id == body.google_product_id,
            PlanPrice.google_base_plan_id == body.google_base_plan_id,
            (PlanPrice.plan_key != plan_key) | (PlanPrice.period != period),
        ))).scalars().first()
        if clash is not None:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT,
                                detail=f"Esse base plan já está ligado a {clash.plan_key}/{clash.period}")
    now = datetime.now(timezone.utc)
    row = (await db.execute(select(PlanPrice).where(PlanPrice.plan_key == plan_key, PlanPrice.period == period))).scalar_one_or_none()
    if row is None:
        row = PlanPrice(id=uuid.uuid4(), plan_key=plan_key, period=period, created_at=now)
        db.add(row)
    for field, value in body.model_dump().items():
        setattr(row, field, value)
    row.updated_at = now
    await db.commit()
    labels = await billing.plan_labels(db)
    return PlanPriceOut(
        plan_key=plan_key, label=labels.get(plan_key, plan_key), period=period,
        price_brl=float(row.price_brl) if row.price_brl is not None else None, is_active=row.is_active,
        google_product_id=row.google_product_id, google_base_plan_id=row.google_base_plan_id,
        updated_by=row.updated_by, updated_at=row.updated_at,
    )


@router.get("/billing/subscriptions", response_model=list[AdminSubscriptionOut])
async def list_subscriptions(
    email: str | None = Query(default=None, max_length=255),
    limit: int = Query(default=100, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
):
    query = select(Subscription, User.email).outerjoin(User, User.id == Subscription.user_id)
    if email:
        query = query.where(User.email == email.strip().lower())
    rows = (await db.execute(query.order_by(Subscription.created_at.desc()).limit(limit))).all()
    return [
        AdminSubscriptionOut(
            id=sub.id, user_email=user_email, plan_key=sub.plan_key, period=sub.period, provider=sub.provider,
            kind=sub.kind, status=sub.status, auto_renew=sub.auto_renew,
            price_brl=float(sub.price_brl) if sub.price_brl is not None else None,
            current_period_start=sub.current_period_start, current_period_end=sub.current_period_end,
            access_until=billing.access_until(sub), canceled_at=sub.canceled_at, ended_at=sub.ended_at,
            created_at=sub.created_at,
        )
        for sub, user_email in rows
    ]
