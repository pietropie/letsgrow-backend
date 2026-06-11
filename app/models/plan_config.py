"""
Configuracao dos planos editavel em runtime via painel admin.

Uma linha por plano (free, jardineiro, cultivador, grower_pro).
Se a tabela estiver vazia, subscription.py faz seed a partir de config.py.
"""
import uuid

from sqlalchemy import Boolean, Integer, Numeric, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

PLAN_KEYS = ("free", "jardineiro", "cultivador", "grower_pro")


class PlanConfig(Base, TimestampMixin):
    __tablename__ = "plan_configs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Identificador do plano -- unico
    plan_key: Mapped[str] = mapped_column(String(30), unique=True, nullable=False)

    # Display
    label: Mapped[str] = mapped_column(String(50), nullable=False)
    price_brl: Mapped[float] = mapped_column(Numeric(8, 2), nullable=False, default=0)
    price_display: Mapped[str] = mapped_column(String(30), nullable=False, default="R$ 0")
    period_display: Mapped[str] = mapped_column(String(30), nullable=False, default="para sempre")
    badge_text: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # Limites
    max_plants: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_grows: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    max_pots_per_grow: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    # null = ilimitado
    ai_queries_per_month: Mapped[int | None] = mapped_column(Integer, nullable=True)
    sensors_allowed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Quem fez a ultima alteracao
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
