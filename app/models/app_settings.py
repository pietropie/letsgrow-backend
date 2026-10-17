"""
Configuracoes de negocio editaveis no painel admin (linha unica).

Nenhuma regra comercial fica no codigo: creditos, carencia de exclusao, caixa de
suporte e padroes de notificacao sao lidos daqui. A migration cria a linha com
padroes neutros (creditos desligados, carencia 7 dias, previa "full").
"""
import uuid

from sqlalchemy import Boolean, Integer, String
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
    updated_by: Mapped[str | None] = mapped_column(String(255), nullable=True)
