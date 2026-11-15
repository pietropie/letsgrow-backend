"""
Configuracoes de negocio editaveis no painel admin (linha unica).

Nenhuma regra comercial fica no codigo: creditos, carencia de exclusao, caixa de
suporte e padroes de notificacao sao lidos daqui. A migration cria a linha com
padroes neutros (creditos desligados, carencia 7 dias, previa "full").
"""
import uuid

from sqlalchemy import Boolean, Float, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

DEFAULT_APP_SETTINGS = {
    "credits_enabled": False,
    "credits_replace_query_limit": False,
    "plan_credits_rollover": False,
    "account_deletion_grace_days": 7,
    "support_inbox_email": None,
    "notification_default_preview": "full",
    "notification_default_frequency": "daily",
    "usage_warning_threshold": 0.8,
    "shop_enabled": False,
    "shop_page_enabled": True,
    "shop_grow_block_enabled": True,
    "shop_plant_block_enabled": True,
    "shop_account_entry_enabled": True,
    "shop_sponsored_enabled": False,
}


class AppSettings(Base, TimestampMixin):
    __tablename__ = "app_settings"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    credits_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    credits_replace_query_limit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    plan_credits_rollover: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    account_deletion_grace_days: Mapped[int] = mapped_column(Integer, nullable=False, default=7)
    support_inbox_email: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # full | discreet
    notification_default_preview: Mapped[str] = mapped_column(String(10), nullable=False, default="full")
    # daily | in_app
    notification_default_frequency: Mapped[str] = mapped_column(String(10), nullable=False, default="daily")
    # Fração do uso a partir da qual GET /usage marca near_limit (o Início avisa)
    usage_warning_threshold: Mapped[float] = mapped_column(Float, nullable=False, default=0.8)
    # Equipamentos e insumos (UX item 26): interruptor geral e um por ponto de exibição.
    # O Bobby não tem interruptor: nunca indica produto (PLAYSTORE.md §2.3, condição 6).
    shop_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    shop_page_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    shop_grow_block_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    shop_plant_block_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    shop_account_entry_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # "Patrocinado" (posição paga) muda o "Contém anúncios" da Play para Sim: desligado por padrão
    shop_sponsored_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
