"""
Assinaturas da conta, precos por periodo e eventos recebidos dos provedores.

O plano e da conta: quem assina pela web (Mercado Pago) usa no Android e vice-versa.
services/billing.py deriva user.plan / user.plan_expires_at da assinatura valida,
entao os limites de subscription.py continuam funcionando sem mudanca.
Precos e IDs de produto do Google Play ficam no admin; nada comercial no codigo.
"""
import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# Periodo -> meses cobrados por ciclo
PLAN_PERIODS = {"monthly": 1, "yearly": 12}

# Estados em que a assinatura da acesso ao plano (ate o fim do periodo pago).
# canceled = renovacao desligada, mas o periodo ja pago continua valendo.
ACCESS_STATUSES = ("active", "grace", "canceled")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class PlanPrice(Base, TimestampMixin):
    """Preco de um plano num periodo. Inativo ou sem preco = nao e vendido."""
    __tablename__ = "plan_prices"
    __table_args__ = (UniqueConstraint("plan_key", "period", name="uq_plan_prices_plan_period"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    plan_key: Mapped[str] = mapped_column(String(30), nullable=False)
    # monthly | yearly
    period: Mapped[str] = mapped_column(String(10), nullable=False)
    # Preco cobrado na web (Mercado Pago). No Android o preco vem do Play Console.
    price_brl: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Assinatura no Play Console (um produto por plano) e o base plan do periodo
    google_product_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    google_base_plan_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)


class Subscription(Base, TimestampMixin):
    __tablename__ = "subscriptions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # SET NULL: registro financeiro e mantido sem vinculo pessoal apos exclusao da conta
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    plan_key: Mapped[str] = mapped_column(String(30), nullable=False)
    period: Mapped[str] = mapped_column(String(10), nullable=False)
    # mercadopago | google_play
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    # recurring (cartao no MP, assinatura no Play) | prepaid (Pix: um periodo pago adiantado)
    kind: Mapped[str] = mapped_column(String(10), nullable=False, default="recurring")
    # MP recurring: id do preapproval; MP prepaid: id do pagamento; Play: purchaseToken
    provider_ref: Mapped[str | None] = mapped_column(String(500), nullable=True, index=True)
    checkout_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    price_brl: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    # pending | active | grace | on_hold | paused | canceled | expired | refunded | replaced | failed
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    auto_renew: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    current_period_start: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    current_period_end: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    canceled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_payment_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)


class PaymentEvent(Base):
    """Notificacoes recebidas (webhook MP, RTDN do Play): idempotencia e auditoria."""
    __tablename__ = "payment_events"
    __table_args__ = (UniqueConstraint("provider", "event_id", name="uq_payment_events_provider_event"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    provider: Mapped[str] = mapped_column(String(20), nullable=False)
    event_id: Mapped[str] = mapped_column(String(200), nullable=False)
    kind: Mapped[str] = mapped_column(String(60), nullable=False)
    payload: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
