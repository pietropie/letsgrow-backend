import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class SensorDevice(Base):
    __tablename__ = "sensor_devices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    plant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("plants.id", ondelete="SET NULL"), nullable=True, index=True
    )

    name: Mapped[str] = mapped_column(String(60), nullable=False)
    esp32_mac: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    firmware_version: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # JSONB describing which sensors are physically connected
    sensors_config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    is_online: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    plant = relationship("Plant", back_populates="sensor_devices")
    readings = relationship(
        "SensorReading", back_populates="device", cascade="all, delete-orphan"
    )


class SensorReading(Base):
    __tablename__ = "sensor_readings"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("sensor_devices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    recorded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    temp_air: Mapped[float | None] = mapped_column(Float, nullable=True)
    humidity_air: Mapped[float | None] = mapped_column(Float, nullable=True)
    co2_ppm: Mapped[int | None] = mapped_column(Integer, nullable=True)
    soil_moisture_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    ph_solution: Mapped[float | None] = mapped_column(Float, nullable=True)
    ec_ms_cm: Mapped[float | None] = mapped_column(Float, nullable=True)
    vpd_kpa: Mapped[float | None] = mapped_column(Float, nullable=True)
    lux: Mapped[int | None] = mapped_column(Integer, nullable=True)
    temp_root: Mapped[float | None] = mapped_column(Float, nullable=True)
    light_leak: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    device = relationship("SensorDevice", back_populates="readings")
