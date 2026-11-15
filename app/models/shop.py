"""
Equipamentos e insumos: indicações de produtos de cultivo com link externo (UX item 26).

Condições da LOJA (analise-2026-10-16/PLAYSTORE.md §2.3): só 10 categorias de
horticultura, só lojas generalistas ou de jardinagem e agro, rótulo de publicidade
em cada cartão, nada no Bobby, e o toque em "Ver na loja" contado só de forma
agregada por produto e dia, sem usuário.
"""
import uuid
from datetime import date, datetime, timezone

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# As 10 categorias permitidas (chave fixa → nome inicial, editável no Admin).
SHOP_CATEGORIES = {
    "light": "Luz",
    "ventilation": "Ventilação",
    "tent": "Tenda",
    "measurement": "Medição",
    "irrigation": "Irrigação",
    "pots": "Vasos",
    "substrate": "Substrato",
    "fertilizer": "Fertilizante",
    "pest_control": "Controle de pragas",
    "tools_ppe": "Ferramentas e EPI",
}
SHOP_LABELS = ("partner", "sponsored", "none")
STORE_KINDS = ("generalist", "garden_agro")


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ShopCategory(Base):
    __tablename__ = "shop_categories"

    key: Mapped[str] = mapped_column(String(30), primary_key=True)
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ShopStore(Base, TimestampMixin):
    """Loja permitida: a URL de cada produto precisa ser deste domínio (ou de um subdomínio)."""
    __tablename__ = "shop_stores"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    domain: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    # generalist | garden_agro
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class ShopProduct(Base, TimestampMixin):
    __tablename__ = "shop_products"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    category_key: Mapped[str] = mapped_column(String(30), ForeignKey("shop_categories.key"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    spec_1: Mapped[str | None] = mapped_column(String(120), nullable=True)
    spec_2: Mapped[str | None] = mapped_column(String(120), nullable=True)
    store_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("shop_stores.id"), nullable=False)
    # Link direto para a página do produto; só a tag de afiliado vai na URL
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    # partner (comissão) | sponsored (posição paga) | none (sem comissão)
    label: Mapped[str] = mapped_column(String(20), nullable=False, default="partner")
    # Gatilhos de contexto (app/services/shop.py: TRIGGERS) que fazem o produto aparecer
    triggers: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Reservados: preço só com fonte e data ("a partir de R$ X, consultado em DD/MM"); nulos por enquanto
    price_from: Mapped[float | None] = mapped_column(Numeric(10, 2), nullable=True)
    price_checked_at: Mapped[date | None] = mapped_column(Date, nullable=True)


class ShopDismissal(Base):
    """"Já tenho": a indicação daquele gatilho some para o ambiente ou a planta."""
    __tablename__ = "shop_dismissals"
    __table_args__ = (UniqueConstraint("user_id", "trigger", "grow_id", "plant_id", name="uq_shop_dismissal"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    trigger: Mapped[str] = mapped_column(String(40), nullable=False)
    grow_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("grows.id", ondelete="CASCADE"), nullable=True)
    plant_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), ForeignKey("plants.id", ondelete="CASCADE"), nullable=True)
    product_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shop_products.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_now)


class ShopClickDaily(Base):
    """Toques em "Ver na loja": só a contagem por produto e dia, sem usuário (não é dado pessoal)."""
    __tablename__ = "shop_click_daily"

    product_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("shop_products.id", ondelete="CASCADE"), primary_key=True
    )
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    clicks: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
