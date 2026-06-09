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

    # --- Iluminação ---
    # led_quantum_board | led_cob | hps_hid | cmh | fluorescent | other
    light_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    light_distance_cm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # "18/6" | "12/12" | "20/4" | "24/0" | "custom"
    photoperiod_hours: Mapped[str | None] = mapped_column(String(10), nullable=True)
    light_leak_controlled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # --- Ventilação ---
    # inline_fan | axial_fan | pc_fans | none
    exhaust_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    carbon_filter: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # active_fan | passive | none
    intake_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    internal_circulation_fans: Mapped[int | None] = mapped_column(Integer, nullable=True)
    negative_pressure: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # --- Controle climático ---
    air_conditioning: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    dehumidifier: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    humidifier: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    heater: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # --- Substrato ---
    # fabric | plastic | air_pot | other
    pot_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # mineral_soil | coco | organic_supersoil | hydro_dwc | other
    substrate_type: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # --- Extensibilidade: sensores ---
    has_environment_sensors: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, server_default="false")

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Legado: Grow não tem mais back_populates em User (renomeado para `plants`)
    user = relationship("User")
    pots = relationship("Pot", back_populates="grow", cascade="all, delete-orphan", order_by="Pot.position")
    conversations = relationship("AIConversation", back_populates="grow")
