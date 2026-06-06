import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class GrowEvent(Base):
    __tablename__ = "grow_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    plant_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plants.id", ondelete="CASCADE"), nullable=False, index=True
    )

    event_type: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    # watering | feeding | pruning | topping | training | transplant
    # flip | observation | harvest | note

    event_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    # Irrigation / feeding data
    ppm: Mapped[float | None] = mapped_column(Float, nullable=True)
    ph_in: Mapped[float | None] = mapped_column(Float, nullable=True)
    ph_out: Mapped[float | None] = mapped_column(Float, nullable=True)
    water_volume_ml: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Environmental / physical measurements
    temperature_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    humidity_rh: Mapped[float | None] = mapped_column(Float, nullable=True)
    weight_g: Mapped[float | None] = mapped_column(Float, nullable=True)

    # Problem severity — enum values: "leve", "moderado", "grave"
    severity: Mapped[str | None] = mapped_column(String(10), nullable=True)

    # Watering — True when the irrigation run contained no nutrients (plain water flush)
    is_flush: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    photo_keys: Mapped[list | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    plant = relationship("Plant", back_populates="events")
