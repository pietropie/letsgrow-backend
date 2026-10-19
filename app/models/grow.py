import uuid
from datetime import date, datetime, time
from typing import Any

from sqlalchemy import Boolean, Date, DateTime, Float, ForeignKey, Integer, String, Text, Time
from sqlalchemy.dialects.postgresql import JSONB, UUID
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
    photoperiod_on_at: Mapped[time | None] = mapped_column(Time, nullable=True)
    light_leak_controlled: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # --- Ventilação ---
    # inline_fan | axial_fan | pc_fans | none
    exhaust_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    exhaust_cfm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exhaust_location: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # Potencia ajustada pela pessoa no controlador fisico (0-100, passos de 5). E so um
    # registro: o app nao comanda o exaustor (nao ha MQTT de saida).
    exhaust_power_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    exhaust_power_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # copa | base
    carbon_filter: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # active_fan | passive | none
    intake_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    internal_circulation_fans: Mapped[int | None] = mapped_column(Integer, nullable=True)
    negative_pressure: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # [{location: "copa"|"base", watts: 25, on: true}]
    fans: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB, nullable=True)

    # --- Controle climático ---
    air_conditioning: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    dehumidifier: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    humidifier: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    heater: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    target_temp_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    target_humidity_rh: Mapped[float | None] = mapped_column(Float, nullable=True)

    # --- Outdoor / greenhouse ---
    lat: Mapped[float | None] = mapped_column(Float, nullable=True)
    lon: Mapped[float | None] = mapped_column(Float, nullable=True)
    sun_exposure: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # full | partial | shade
    rain_protection: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # none | cover | greenhouse

    # --- Substrato (default do grow; cada planta pode override) ---
    # fabric | plastic | air_pot | other
    pot_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # mineral_soil | coco | organic_supersoil | hydro_dwc | other
    substrate_type: Mapped[str | None] = mapped_column(String(30), nullable=True)

    has_environment_sensors: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False, server_default="false"
    )

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    user = relationship("User")
    plants = relationship("Plant", back_populates="grow")
    pots = relationship("Pot", back_populates="grow", cascade="all, delete-orphan", order_by="Pot.position")
    sensor_devices = relationship("SensorDevice", back_populates="grow")
    conversations = relationship("AIConversation", back_populates="grow")
