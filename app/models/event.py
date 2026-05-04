import uuid
from datetime import datetime

from sqlalchemy import DateTime, Float, ForeignKey, String, Text, func
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

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    photo_keys: Mapped[list | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    plant = relationship("Plant", back_populates="events")
