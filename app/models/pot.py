import uuid

from sqlalchemy import Float, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Pot(Base):
    __tablename__ = "pots"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    grow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("grows.id", ondelete="CASCADE"), nullable=False, index=True
    )

    label: Mapped[str] = mapped_column(String(60), nullable=False)
    volume_liters: Mapped[float] = mapped_column(Float, default=7.0, nullable=False)
    substrate: Mapped[str | None] = mapped_column(String(100), nullable=True)
    position: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    grow = relationship("Grow", back_populates="pots")
