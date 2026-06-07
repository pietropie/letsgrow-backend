import uuid
from datetime import datetime

from sqlalchemy import DateTime, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSON, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Strain(Base):
    """Catálogo de strains indexado a partir de Brain/wiki/strains/*.md.

    Usado para autocomplete (criação de planta) e para o "card de informações
    da strain" exibido a partir de `Plant.strain_name`.
    """

    __tablename__ = "strains"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    name: Mapped[str] = mapped_column(String(150), nullable=False, index=True)
    # slug derivado do nome do arquivo .md — usado como chave natural para upsert idempotente
    slug: Mapped[str] = mapped_column(String(170), nullable=False, unique=True, index=True)

    aliases: Mapped[list | None] = mapped_column(JSON, nullable=True)
    # lista de apelidos/variações de grafia, ex: ["gelato 41", "gelato#41", "G41"]

    strain_type: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # photo | auto — inferido de tags/aliases (ex: "auto", "automática")
    genetics: Mapped[str | None] = mapped_column(String(20), nullable=True)
    # indica | sativa | hybrid — derivado de `dominancia` (indica/sativa/hibrida)

    breeder: Mapped[str | None] = mapped_column(String(150), nullable=True)
    thc_pct: Mapped[str | None] = mapped_column(String(30), nullable=True)
    cbd_pct: Mapped[str | None] = mapped_column(String(30), nullable=True)
    dominant_terpene: Mapped[str | None] = mapped_column(String(60), nullable=True)
    flowering_days: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height_cm: Mapped[str | None] = mapped_column(String(30), nullable=True)

    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    # corpo da seção "## Resumo", truncado a ~600 chars

    source_file: Mapped[str] = mapped_column(String(255), nullable=False)
    # caminho relativo do .md de origem (ex: "strains/gelato-41.md")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
