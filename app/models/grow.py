import uuid
from datetime import date, datetime

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Grow(Base, TimestampMixin):
    __tablename__ = "grows"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    name: Mapped[str] = mapped_column(String(100), nullable=False)
    grow_type: Mapped[str] = mapped_column(String(20), default="indoor", nullable=False)
    # indoor | outdoor | greenhouse
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)
    # active | harvested | abandoned

    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date | None] = mapped_column(Date, nullable=True)

    # Tent dimensions (cm)
    tent_width_cm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tent_depth_cm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tent_height_cm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    lighting_watts: Mapped[int | None] = mapped_column(Integer, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Legado: Grow não tem mais back_populates em User (renomeado para `plants`)
    user = relationship("User")
    pots = relationship("Pot", back_populates="grow", cascade="all, delete-orphan", order_by="Pot.position")
    conversations = relationship("AIConversation", back_populates="grow")
