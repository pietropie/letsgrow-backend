"""Preferencias de acompanhamento (push do resumo diario) por usuario."""
import uuid
from datetime import time

from sqlalchemy import ForeignKey, String, Time
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class NotificationPreference(Base, TimestampMixin):
    __tablename__ = "notification_preferences"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # daily = resumo diario por push | in_app = somente dentro do app
    frequency: Mapped[str] = mapped_column(String(10), nullable=False)
    brief_time: Mapped[time] = mapped_column(Time, nullable=False)
    # IANA, ex. "America/Sao_Paulo" (enviado pelo aparelho)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    quiet_start: Mapped[time | None] = mapped_column(Time, nullable=True)
    quiet_end: Mapped[time | None] = mapped_column(Time, nullable=True)
    # full | discreet
    preview: Mapped[str] = mapped_column(String(10), nullable=False)
