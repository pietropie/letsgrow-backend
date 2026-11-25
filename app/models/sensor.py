import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, CheckConstraint, DateTime, Float, ForeignKey, Index, Integer, SmallInteger,
    String, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class SensorDevice(Base):
    __tablename__ = "sensor_devices"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    plant_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("plants.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    grow_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("grows.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

    name: Mapped[str] = mapped_column(String(60), nullable=False)
    esp32_mac: Mapped[str] = mapped_column(String(20), unique=True, nullable=False)
    firmware_version: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Hub+Satellite ESP-NOW topology fields
    # 'hub' | 'satellite' | 'standalone' (legacy default)
    module_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # MAC address of the parent hub; populated only for satellites
    hub_mac: Mapped[str | None] = mapped_column(String(17), nullable=True, index=True)
    # FALSE while the grower has not assigned this device to a plant yet
    is_paired: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # JSONB describing which sensors are physically connected
    sensors_config: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    # Tipo legível: air | soil | light | solution | relay | hub (app/services/sensor_catalog.py)
    sensor_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # --- Hub: relés (contrato reles-app-plano.md) ---
    # Tensão da tomada onde o Hub está ligado (127 ou 220); null conta como 127 nos avisos
    mains_voltage: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    # Versão da configuração dos relés publicada e a última confirmada pelo Hub (config_ack)
    relay_rev: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    relay_synced_rev: Mapped[int | None] = mapped_column(Integer, nullable=True)
    relay_sync_error: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Relógio do Hub informado no status: ntp | rtc | none
    clock_source: Mapped[str | None] = mapped_column(String(8), nullable=True)

    is_online: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    plant = relationship("Plant", back_populates="sensor_devices")
    grow = relationship("Grow", back_populates="sensor_devices")
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


class HubClaimCode(Base):
    """Código de vínculo de um Hub novo (onboarding por BLE).

    O app pede o código (POST /iot/hubs/claim-codes) e o manda ao Hub junto com o
    Wi-Fi; o Hub troca o código pela senha MQTT (POST /iot/hubs/activate). Vale
    15 min e uma vez só. Guardamos só o SHA-256 do código.
    """

    __tablename__ = "hub_claim_codes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    code_hash: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    grow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("grows.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # MAC (12 hex maiúsculos) do Hub que usou o código
    used_by_mac: Mapped[str | None] = mapped_column(String(12), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SensorModel(Base):
    """Foto por modelo de sensor (catálogo de hardware), não por aparelho.

    `key` é o `sensors_config.model` do aparelho ou, sem ele, o module_type do firmware
    (solo, ambiente, ppfd, nutricao, automacao) e "hub". Sem foto do modelo, vale a do
    module_type; sem as duas, a API devolve `photo_url: null` e o app mostra o ícone.
    """

    __tablename__ = "sensor_models"

    key: Mapped[str] = mapped_column(String(40), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    sensor_kind: Mapped[str | None] = mapped_column(String(20), nullable=True)
    photo_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class HubRelay(Base):
    """Uma das 4 saídas de relé do Hub: vínculo com o equipamento do ambiente, modo e o
    último estado conhecido."""

    __tablename__ = "hub_relays"
    __table_args__ = (
        UniqueConstraint("hub_device_id", "n", name="uq_hub_relays_hub_n"),
        CheckConstraint("n BETWEEN 1 AND 4", name="ck_hub_relays_n"),
        # Um equipamento em uma posição só, contando todos os Hubs do ambiente
        Index(
            "uq_hub_relays_grow_equip", "grow_id", "equip_kind", "equip_key", unique=True,
            postgresql_where=text("equip_key IS NOT NULL"), sqlite_where=text("equip_key IS NOT NULL"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hub_device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sensor_devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    grow_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("grows.id", ondelete="CASCADE"), nullable=False, index=True
    )
    n: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # light | exhaust | fan; null = posição livre
    equip_kind: Mapped[str | None] = mapped_column(String(10), nullable=True)
    # light | exhaust | fan:<id>; é também o equip_ref mandado ao Hub
    equip_key: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # manual | schedule | humidity
    mode: Mapped[str] = mapped_column(String(10), default="manual", server_default="manual", nullable=False)
    # Programação própria (exaustor e ventiladores); a luz usa o fotoperíodo do ambiente
    schedule_on: Mapped[str | None] = mapped_column(String(5), nullable=True)
    schedule_off: Mapped[str | None] = mapped_column(String(5), nullable=True)
    hum_on_above: Mapped[float | None] = mapped_column(Float, nullable=True)
    hum_off_below: Mapped[float | None] = mapped_column(Float, nullable=True)
    min_on_s: Mapped[int] = mapped_column(Integer, default=60, server_default="60", nullable=False)
    min_off_s: Mapped[int] = mapped_column(Integer, default=60, server_default="60", nullable=False)
    # on | off; null = padrão do Hub (exaustor e ventilador ligados, o resto desligado)
    failsafe: Mapped[str | None] = mapped_column(String(3), nullable=True)

    # Último estado conhecido; state_confirmed=False enquanto o Hub não confirmou
    state: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    override: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    state_source: Mapped[str | None] = mapped_column(String(20), nullable=True)
    state_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    state_confirmed: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class RelayCommand(Base):
    """Comando manual de uma posição (relay_set / relay_resume) e a confirmação do Hub."""

    __tablename__ = "relay_commands"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    req_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True, nullable=False, default=uuid.uuid4)
    hub_device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sensor_devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    n: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # set | resume
    action: Mapped[str] = mapped_column(String(10), nullable=False)
    requested_state: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    duration_s: Mapped[int] = mapped_column(Integer, default=0, server_default="0", nullable=False)
    # pending | confirmed | failed (timeout é calculado na leitura)
    status: Mapped[str] = mapped_column(String(10), default="pending", server_default="pending", nullable=False)
    err: Mapped[str | None] = mapped_column(String(30), nullable=True)
    confirmed_state: Mapped[int | None] = mapped_column(SmallInteger, nullable=True)
    override: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class RelayEvent(Base):
    """Evento publicado pelo Hub em relay/event. `seq` é único por Hub: o Hub reenvia a fila
    ao reconectar e o QoS 1 pode repetir a mensagem."""

    __tablename__ = "relay_events"
    __table_args__ = (
        UniqueConstraint("hub_device_id", "seq", name="uq_relay_events_hub_seq"),
        Index("ix_relay_events_grow_received", "grow_id", "received_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    hub_device_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("sensor_devices.id", ondelete="CASCADE"), nullable=False, index=True
    )
    grow_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("grows.id", ondelete="CASCADE"), nullable=True
    )
    n: Mapped[int] = mapped_column(SmallInteger, nullable=False)  # 0 = o Hub inteiro
    state: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    source: Mapped[str] = mapped_column(String(20), nullable=False)
    seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    hub_ts: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)  # null: Hub sem hora
    hum: Mapped[float | None] = mapped_column(Float, nullable=True)
    equip_ref: Mapped[str | None] = mapped_column(String(40), nullable=True)
    req_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
