import uuid
from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base, TimestampMixin


class Plant(Base, TimestampMixin):
    __tablename__ = "plants"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    pot_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("pots.id", ondelete="CASCADE"), nullable=False, unique=True
    )

    strain_name: Mapped[str] = mapped_column(String(100), nullable=False)
    strain_type: Mapped[str] = mapped_column(String(20), default="photo", nullable=False)
    # photo | auto
    genetics: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # indica | sativa | hybrid
    seed_type: Mapped[str] = mapped_column(String(20), default="feminized", nullable=False)
    # feminized | regular | autoflower

    germination_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    current_phase: Mapped[str] = mapped_column(String(20), default="germination", nullable=False)
    # germination | seedling | veg | flower | harvest | done

    flip_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    harvest_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    expected_harvest_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    pot = relationship("Pot", back_populates="plant")
    events = relationship(
        "GrowEvent", back_populates="plant", cascade="all, delete-orphan", order_by="GrowEvent.event_date.desc()"
    )
