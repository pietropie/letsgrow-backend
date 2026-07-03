import uuid
from datetime import datetime

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, func
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
    # watering | feeding | pruning | training | transplant | flip | observation
    # environment | trichome_reading | symptom | harvest | drying | curing | note

    event_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )

    # -- Rega: entrada
    ppm: Mapped[float | None] = mapped_column(Float, nullable=True)
    ph_in: Mapped[float | None] = mapped_column(Float, nullable=True)
    water_volume_ml: Mapped[float | None] = mapped_column(Float, nullable=True)

    # -- Rega: saida (runoff) -- diagnostico de acumulo de sais e pH do solo
    ph_out: Mapped[float | None] = mapped_column(Float, nullable=True)
    ec_out: Mapped[float | None] = mapped_column(Float, nullable=True)
    has_runoff: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    # -- Nutricao: subtipo
    # "base" | "booster" | "suplemento" | "foliar"
    nutrient_subtype: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # -- Treinamento: subtipo
    # "topping" | "fim" | "lst" | "supercropping" | "lollipopping" | "schwazzing"
    training_subtype: Mapped[str | None] = mapped_column(String(30), nullable=True)

    # -- Tricomas: % de cada estagio para decisao de colheita
    trichome_clear_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trichome_milky_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)
    trichome_amber_pct: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # -- Ambiente e medicoes fisicas
    temperature_c: Mapped[float | None] = mapped_column(Float, nullable=True)
    humidity_rh: Mapped[float | None] = mapped_column(Float, nullable=True)
    weight_g: Mapped[float | None] = mapped_column(Float, nullable=True)

    # -- Diagnostico
    severity: Mapped[str | None] = mapped_column(String(10), nullable=True)

    # Watering -- True quando nao levou nutrientes (flush ou agua pura)
    is_flush: Mapped[bool | None] = mapped_column(Boolean, nullable=True)

    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    photo_keys: Mapped[list | None] = mapped_column(JSON, nullable=True)

    # -- Catch-all para campos semi-estruturados
    # Ex: {"harvest_method": "wet_trim", "symptom_type": "amarelamento",
    #      "symptom_location": "folhas velhas", "soil_wet": true,
    #      "node_number": 4, "drying_temp_c": 19, "jar_humidity_rh": 62,
    #      "dry_crack_test": true, "defoliation_type": "schwazzing"}
    # ATENCAO: "metadata" e reservado pelo SQLAlchemy Declarative API.
    # Atributo Python = event_metadata; coluna no banco = "metadata".
    event_metadata: Mapped[dict | None] = mapped_column("metadata", JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    plant = relationship("Plant", back_populates="events")
