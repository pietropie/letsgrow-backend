"""
Creditos: custos por tarefa, pacotes, extrato, cotacoes/reservas e compras.

Valores comerciais vem do admin. O extrato e imutavel: cada credito entra por um
lote (grant_plan ou purchase) e sai por lancamentos negativos que apontam para o
lote (reserve, expire, adjust). release devolve ao mesmo lote.
"""
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import (
    Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# Tarefas que o backend executa e que podem ter custo configurado no admin.
CREDIT_TASKS = {
    "chat_message": "Mensagem ao Bobby",
    "photo_analysis": "Análise de foto",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


class CreditTaskCost(Base, TimestampMixin):
    __tablename__ = "credit_task_costs"

    task_key: Mapped[str] = mapped_column(String(40), primary_key=True)
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)


class CreditPackage(Base, TimestampMixin):
    __tablename__ = "credit_packages"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    price_brl: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    # null = sem validade
    validity_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # Produto consumivel no Play Console (compra no Android); null = nao vendido no Android
    google_product_id: Mapped[str | None] = mapped_column(String(100), nullable=True, unique=True)
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)


class CreditQuote(Base):
    """Cotacao mostrada antes da tarefa; vira reserva quando o usuario confirma."""
    __tablename__ = "credit_quotes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_key: Mapped[str] = mapped_column(String(40), nullable=False)
    cost: Mapped[int] = mapped_column(Integer, nullable=False)
    scope: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    # quoted | reserved | captured | released | expired
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="quoted")
    task_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(200), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CreditPurchase(Base, TimestampMixin):
    __tablename__ = "credit_purchases"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    # SET NULL: registro de compra e mantido sem vinculo pessoal apos exclusao da conta
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    package_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("credit_packages.id", ondelete="SET NULL"), nullable=True
    )
    label: Mapped[str] = mapped_column(String(80), nullable=False)
    credits: Mapped[int] = mapped_column(Integer, nullable=False)
    price_brl: Mapped[float] = mapped_column(Numeric(10, 2), nullable=False)
    validity_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    # MP: "credits:<id>" (external_reference); Play: purchaseToken
    provider_ref: Mapped[str | None] = mapped_column(String(500), nullable=True, index=True)
    checkout_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    # pending | paid | failed | canceled | refunded | charged_back
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="pending")
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CreditLedgerEntry(Base):
    __tablename__ = "credit_ledger"
    __table_args__ = (
        # Um lote de plano por usuario e periodo (period_start so existe em grant_plan).
        UniqueConstraint("user_id", "period_start", name="uq_credit_ledger_plan_period"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # grant_plan | purchase | reserve | capture | release | expire | adjust
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    # plan | extra
    bucket: Mapped[str] = mapped_column(String(10), nullable=False)
    amount: Mapped[int] = mapped_column(Integer, nullable=False)
    # Lote de origem (para lancamentos que consomem/devolvem); nulo nos proprios lotes.
    lot_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("credit_ledger.id", ondelete="CASCADE"), nullable=True, index=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    period_start: Mapped[date | None] = mapped_column(Date, nullable=True)
    quote_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("credit_quotes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    purchase_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("credit_purchases.id", ondelete="SET NULL"), nullable=True
    )
    task_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    task_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)
    description: Mapped[str] = mapped_column(String(200), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, nullable=False)
